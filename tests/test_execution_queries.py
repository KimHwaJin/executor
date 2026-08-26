from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    CreateOperationCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    FailureType,
    OperationMode,
    RetryStrategy,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    RuntimeType,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import OutboxEvent, utc_now
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_queries import (
    SQLAlchemyExecutionQueryService,
)
from tests.runtime_credentials import runtime_credential_fields


def _submit_command() -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key="trace-submit",
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="trace-user",
        project_id="trace-project",
        session_id="trace-session",
        task_id="test-task",
        steps=(
            StepSpec(
                sequence=0,
                code="print('trace')",
                skill_name="data_load",
                tool_name="load_data",
            ),
        ),
    )


async def test_query_service_returns_attempt_step_and_redacted_events(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(_submit_command())
    now = utc_now()
    target_id = uuid4()
    attempt_id = uuid4()
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                id=target_id,
                name="trace-jupyter",
                connection_config={"endpoint": "http://127.0.0.1:8888"},
                **runtime_credential_fields(),
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
                max_concurrent_executions=1,
                supported_profiles=["basic"],
                enabled=True,
            )
        )
        session.add(
            ExecutionAttemptORM(
                id=attempt_id,
                execution_id=execution.id,
                attempt_number=1,
                runtime_target_id=target_id,
                runtime_session_id="kernel-1",
                status=AttemptStatus.FAILED,
                lease_owner="worker-1",
                lease_expires_at=now + timedelta(minutes=1),
                heartbeat_at=now,
                error_message="expected failure",
                failure_type=FailureType.TOOL_ERROR,
                retry_strategy=RetryStrategy.FROM_FAILED_STEP,
                runtime_session_cleanup_status=RuntimeSessionCleanupStatus.NOT_REQUIRED,
                started_at=now,
                finished_at=now,
            )
        )
        session.add(
            ExecutionStepAttemptORM(
                execution_id=execution.id,
                execution_attempt_id=attempt_id,
                execution_step_id=execution.steps[0].id,
                sequence=0,
                skill_name="data_load",
                tool_name="load_data",
                input_parameters={"source": "daily"},
                status=StepStatus.FAILED,
                output_summary={
                    "output_count": 1,
                    "output_types": {"error": 1},
                    "stream_names": [],
                    "mime_types": [],
                    "has_image": False,
                    "image_count": 0,
                    "has_error": True,
                },
                error_message="expected failure",
                started_at=now,
                finished_at=now,
            )
        )
        session.add(
            OutboxEventORM.from_domain(
                OutboxEvent(
                    aggregate_type="Execution",
                    aggregate_id=execution.id,
                    event_type="execution.test_secret",
                    event_sequence=1,
                    payload={
                        "token": "must-not-leak",
                        "nested": {"password": "hidden"},
                    },
                )
            )
        )
        for sequence in (2, 3):
            session.add(
                OutboxEventORM.from_domain(
                    OutboxEvent(
                        aggregate_type="Execution",
                        aggregate_id=execution.id,
                        event_type="execution.test_sequence",
                        event_sequence=sequence,
                        payload={"sequence": sequence},
                    )
                )
            )

    queries = SQLAlchemyExecutionQueryService(session_factory)
    attempts = await queries.attempts(execution.id)
    attempt = await queries.attempt(execution.id, attempt_id)
    attempt_steps = await queries.attempt_steps(execution.id, attempt_id)
    events = await queries.events(execution.id)
    recovery_page = await queries.events(
        execution.id, after_sequence=1, limit=1
    )
    recovery_next = await queries.events(
        execution.id, cursor=recovery_page.next_cursor, limit=1
    )

    assert len(attempts) == 1
    assert attempts[0].status == AttemptStatus.FAILED
    assert attempts[0].runtime_type == RuntimeType.JUPYTER
    assert attempts[0].runtime_profile == "basic"
    assert attempts[0].failure_type == FailureType.TOOL_ERROR
    assert attempts[0].retry_strategy == RetryStrategy.FROM_FAILED_STEP
    assert attempts[0].step_count == 1
    assert attempt.step_count == 1
    assert attempt_steps[0].tool_name == "load_data"
    assert attempt_steps[0].output_summary["output_count"] == 1
    assert attempt_steps[0].output_summary["has_error"] is True
    secret_event = next(
        event
        for event in events
        if event.event_type == "execution.test_secret"
    )
    assert secret_event.payload == {
        "token": "[REDACTED]",
        "nested": {"password": "[REDACTED]"},
    }
    assert [event.event_sequence for event in events] == [1, 2, 3]
    assert [event.event_sequence for event in recovery_page] == [2]
    assert recovery_page.next_cursor is not None
    assert [event.event_sequence for event in recovery_next] == [3]


async def test_execution_reads_do_not_load_source_code_or_step_rows(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(_submit_command())
    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement.lower())

    event.listen(
        engine.sync_engine, "before_cursor_execute", capture_statement
    )
    try:
        queries = SQLAlchemyExecutionQueryService(
            create_session_factory(engine)
        )
        page = await queries.executions(user_id=execution.user_id)
        detail = await queries.execution(execution.id)
    finally:
        event.remove(
            engine.sync_engine, "before_cursor_execute", capture_statement
        )

    assert page[0].step_count == 1
    assert detail.id == execution.id
    normalized = [" ".join(statement.split()) for statement in statements]
    assert len(normalized) == 2
    assert all(
        "execution_steps.code" not in statement for statement in normalized
    )
    execution_selects = [
        statement
        for statement in normalized
        if " from executions " in statement
    ]
    assert execution_selects
    assert all(
        "executions.code," not in statement for statement in execution_selects
    )


async def test_result_snapshot_uses_fixed_bulk_queries_and_workflow_filter(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    command = replace(
        _submit_command(),
        idempotency_key="bulk-result-submit",
        operation_mode=OperationMode.MULTI,
        operation_wait_timeout_seconds=600,
        workflow_id="workflow-bulk",
    )
    execution = await execution_service.submit(command)
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(status=ExecutionStatus.WAITING_FOR_OPERATION, version=2)
        )
    await execution_service.create_operation(
        CreateOperationCommand(
            execution_id=execution.id,
            idempotency_key="bulk-result-operation",
            expected_version=2,
            steps=(StepSpec(sequence=1, code="print('next')"),),
        )
    )

    queries = SQLAlchemyExecutionQueryService(session_factory)
    filtered = await queries.executions(workflow_id="workflow-bulk")
    assert [item.id for item in filtered.items] == [execution.id]
    assert not (await queries.executions(workflow_id="other")).items

    statements: list[str] = []

    def capture_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement.lower())

    event.listen(
        engine.sync_engine, "before_cursor_execute", capture_statement
    )
    try:
        snapshot = await queries.execution_result_snapshot(execution.id)
    finally:
        event.remove(
            engine.sync_engine, "before_cursor_execute", capture_statement
        )

    assert len(snapshot.operations) == 2
    assert len(snapshot.steps) == 2
    assert len(statements) == 6
