from datetime import timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import StepSpec, SubmitExecutionCommand
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    AttemptStatus,
    CodeSourceType,
    ExecutionMode,
    FailureType,
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
    ExecutionStepAttemptORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_queries import SQLAlchemyExecutionQueryService


def _submit_command() -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key="trace-submit",
        mode=ExecutionMode.STATIC,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        code_source_type=CodeSourceType.INLINE,
        source_content="print('trace')",
        code_path=None,
        source_sha256="0" * 64,
        user_id="trace-user",
        project_id="trace-project",
        session_id="trace-session",
        task_id="test-task",
        execution_plan_id="trace-plan",
        steps=(
            StepSpec(
                sequence=0,
                code="print('trace')",
                execution_plan_id="trace-plan",
                plan_step_id="trace-plan-step-0",
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
                credential_ref="settings:JUPYTER_TOKEN",
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
                    payload={"token": "must-not-leak", "nested": {"password": "hidden"}},
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
    secret_event = next(event for event in events if event.event_type == "execution.test_secret")
    assert secret_event.payload == {
        "token": "[REDACTED]",
        "nested": {"password": "[REDACTED]"},
    }
