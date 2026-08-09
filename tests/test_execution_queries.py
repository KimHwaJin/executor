from datetime import timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import StepSpec, SubmitExecutionCommand
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    AttemptStatus,
    CodeSourceType,
    ExecutionMode,
    JupyterPool,
    JupyterServerStatus,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import OutboxEvent, utc_now
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionStepAttemptORM,
    JupyterServerORM,
    OutboxEventORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_queries import SQLAlchemyExecutionQueryService


def _submit_command() -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key="trace-submit",
        mode=ExecutionMode.STATIC,
        trigger_type=TriggerType.INTERACTIVE,
        jupyter_pool=JupyterPool.INTERACTIVE,
        kernel_name="python3",
        code_source_type=CodeSourceType.INLINE,
        code="print('trace')",
        code_path=None,
        requested_by_user_id="trace-user",
        project_id="trace-project",
        session_id="trace-session",
        execution_plan_id="trace-plan",
        steps=(StepSpec(sequence=0, skill_name="data_load", tool_name="load_data"),),
    )


async def test_query_service_returns_attempt_step_and_redacted_event_trace(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(_submit_command())
    now = utc_now()
    server_id = uuid4()
    attempt_id = uuid4()
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(
            JupyterServerORM(
                id=server_id,
                name="trace-jupyter",
                endpoint="http://127.0.0.1:8888",
                credential_ref="settings:JUPYTER_TOKEN",
                pool=JupyterPool.INTERACTIVE,
                status=JupyterServerStatus.ACTIVE,
                max_concurrent_executions=1,
                supported_kernels=["python3"],
                enabled=True,
            )
        )
        session.add(
            ExecutionAttemptORM(
                id=attempt_id,
                execution_id=execution.id,
                attempt_number=1,
                jupyter_server_id=server_id,
                kernel_id="kernel-1",
                status=AttemptStatus.FAILED,
                lease_owner="worker-1",
                lease_expires_at=now + timedelta(minutes=1),
                heartbeat_at=now,
                error_message="expected failure",
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
    events = await queries.events(execution.id)
    trace = await queries.trace(execution.id)

    assert len(attempts) == 1
    assert attempts[0].status == AttemptStatus.FAILED
    assert attempts[0].steps[0].tool_name == "load_data"
    assert attempts[0].steps[0].outputs == [{"output_type": "error"}]
    secret_event = next(event for event in events if event.event_type == "execution.test_secret")
    assert secret_event.payload == {
        "token": "[REDACTED]",
        "nested": {"password": "[REDACTED]"},
    }
    assert trace.execution.id == execution.id
    assert trace.attempts == tuple(attempts)
