from datetime import timedelta
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    AttemptStatus,
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
    ExecutionOutputJournalORM,
    ExecutionOutputORM,
    ExecutionOutputRepresentationORM,
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
                outputs=[{"output_type": "error"}],
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
                    payload={
                        "token": "must-not-leak",
                        "nested": {"password": "hidden"},
                    },
                )
            )
        )

    queries = SQLAlchemyExecutionQueryService(session_factory)
    attempts = await queries.attempts(execution.id)
    attempt = await queries.attempt(execution.id, attempt_id)
    attempt_steps = await queries.attempt_steps(execution.id, attempt_id)
    events = await queries.events(execution.id)

    assert len(attempts) == 1
    assert attempts[0].status == AttemptStatus.FAILED
    assert attempts[0].runtime_type == RuntimeType.JUPYTER
    assert attempts[0].runtime_profile == "basic"
    assert attempts[0].failure_type == FailureType.TOOL_ERROR
    assert attempts[0].retry_strategy == RetryStrategy.FROM_FAILED_STEP
    assert attempts[0].step_count == 1
    assert attempt.step_count == 1
    assert attempt_steps[0].tool_name == "load_data"
    assert attempt_steps[0].outputs == [{"output_type": "error"}]
    secret_event = next(
        event
        for event in events
        if event.event_type == "execution.test_secret"
    )
    assert secret_event.payload == {
        "token": "[REDACTED]",
        "nested": {"password": "[REDACTED]"},
    }


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


async def test_output_queries_page_filter_and_redact_metadata(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(_submit_command())
    now = utc_now()
    target_id = uuid4()
    attempt_id = uuid4()
    step_attempt_id = uuid4()
    journal_id = uuid4()
    output_ids = [
        uuid4(),
        uuid4(),
    ]
    operation_id = execution.steps[0].operation_id
    assert operation_id is not None
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                id=target_id,
                name="output-jupyter",
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
                runtime_session_id="kernel-output",
                status=AttemptStatus.SUCCEEDED,
                heartbeat_at=now,
                retry_strategy=RetryStrategy.FROM_FAILED_STEP,
                runtime_session_cleanup_status=(
                    RuntimeSessionCleanupStatus.NOT_REQUIRED
                ),
                started_at=now,
                finished_at=now,
            )
        )
        session.add(
            ExecutionStepAttemptORM(
                id=step_attempt_id,
                execution_id=execution.id,
                execution_attempt_id=attempt_id,
                execution_step_id=execution.steps[0].id,
                sequence=0,
                status=StepStatus.SUCCEEDED,
                outputs=[],
                started_at=now,
                finished_at=now,
            )
        )
        session.add(
            ExecutionOutputJournalORM(
                id=journal_id,
                execution_id=execution.id,
                operation_id=operation_id,
                execution_step_id=execution.steps[0].id,
                execution_attempt_id=attempt_id,
                execution_step_attempt_id=step_attempt_id,
                runtime_target_id=target_id,
                runtime_session_id="kernel-output",
                workspace_path="users/u/executions/e",
                sequence=0,
                fencing_token=3,
                state="FINALIZED",
                committed_offset=2,
                output_count=2,
                representation_count=2,
                total_bytes=8,
                checksum_sha256="c" * 64,
            )
        )
        for ordinal, output_id in enumerate(output_ids):
            session.add(
                ExecutionOutputORM(
                    id=output_id,
                    journal_id=journal_id,
                    batch_id=uuid4(),
                    execution_id=execution.id,
                    operation_id=operation_id,
                    execution_step_id=execution.steps[0].id,
                    execution_attempt_id=attempt_id,
                    sequence=0,
                    ordinal=ordinal,
                    kind="STREAM",
                    stream_name="stdout",
                    output_metadata={"token": "hidden"},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ExecutionOutputRepresentationORM(
                    id=uuid4(),
                    output_id=output_id,
                    media_type="text/plain",
                    size_bytes=4,
                    checksum_sha256="a" * 64,
                    complete=True,
                    content_ref=f"journal://{journal_id}/{output_id}/text",
                    representation_metadata={"password": "hidden"},
                    created_at=now,
                    updated_at=now,
                )
            )

    queries = SQLAlchemyExecutionQueryService(session_factory)
    first = await queries.outputs(execution.id, attempt_id=attempt_id, limit=1)
    second = await queries.outputs(
        execution.id, cursor=first.next_cursor, limit=1
    )
    detail = await queries.output(execution.id, first.items[0].id)

    assert len(first.items) == 1
    assert first.next_cursor is not None
    assert len(second.items) == 1
    assert first.items[0].id != second.items[0].id
    assert detail.journal_id == journal_id
    assert detail.runtime_target_id == target_id
    assert detail.metadata == {"token": "[REDACTED]"}
    assert detail.representations[0].metadata == {"password": "[REDACTED]"}
