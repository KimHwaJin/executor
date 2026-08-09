from datetime import timedelta
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import StepSpec, SubmitExecutionCommand
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import (
    AttemptStatus,
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    FailureType,
    JupyterPool,
    JupyterServerStatus,
    KernelCleanupStatus,
    RetryStrategy,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    JupyterServerORM,
    OutboxEventORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.jupyter_registry import JupyterServerRegistry
from executor_service.infrastructure.worker import ExecutionWorker


def _command() -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key="lease-recovery-submit",
        mode=ExecutionMode.STATIC,
        trigger_type=TriggerType.INTERACTIVE,
        jupyter_pool=JupyterPool.INTERACTIVE,
        kernel_name="python3",
        code_source_type=CodeSourceType.INLINE,
        code="print('long-running')",
        code_path=None,
        requested_by_user_id="recovery-user",
        project_id="recovery-project",
        session_id="recovery-session",
        execution_plan_id="recovery-plan",
        steps=(StepSpec(sequence=0, tool_name="long_running_tool"),),
    )


async def test_expired_lease_is_failed_once_and_can_restart_from_zero(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    execution = await execution_service.submit(_command())
    now = utc_now()
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        server = JupyterServerORM(
            name="recovery-jupyter",
            endpoint="http://127.0.0.1:9",
            credential_ref="settings:JUPYTER_TOKEN",
            pool=JupyterPool.INTERACTIVE,
            status=JupyterServerStatus.ACTIVE,
            max_concurrent_executions=1,
            supported_kernels=["python3"],
            enabled=True,
        )
        session.add(server)
        await session.flush()
        attempt = ExecutionAttemptORM(
            execution_id=execution.id,
            attempt_number=1,
            jupyter_server_id=server.id,
            status=AttemptStatus.RUNNING,
            lease_owner="dead-worker",
            lease_expires_at=now - timedelta(seconds=1),
            heartbeat_at=now - timedelta(minutes=1),
            started_at=now - timedelta(minutes=2),
        )
        session.add(attempt)
        await session.flush()
        session.add(
            ExecutionStepAttemptORM(
                execution_id=execution.id,
                execution_attempt_id=attempt.id,
                execution_step_id=execution.steps[0].id,
                sequence=0,
                tool_name="long_running_tool",
                input_parameters={},
                status=StepStatus.RUNNING,
                outputs=[],
                started_at=now - timedelta(minutes=2),
            )
        )
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.RUNNING,
                jupyter_server_id=server.id,
                kernel_id=None,
                lease_owner="dead-worker",
                lease_expires_at=now - timedelta(seconds=1),
                heartbeat_at=now - timedelta(minutes=1),
                started_at=now - timedelta(minutes=2),
            )
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.id == execution.steps[0].id)
            .values(status=StepStatus.RUNNING, started_at=now - timedelta(minutes=2))
        )

    settings = Settings(
        jupyter_enabled=False,
        workspace_host_root=tmp_path,
        execution_lease_seconds=30,
        execution_heartbeat_seconds=5,
    )
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    registry = JupyterServerRegistry(session_factory, settings)
    worker = ExecutionWorker(
        session_factory=session_factory,
        redis=redis,
        settings=settings,
        registry=registry,
        artifact_manager=ExecutionArtifactManager(session_factory, settings),
    )
    try:
        await worker._recover_expired_leases()
        await worker._recover_expired_leases()
    finally:
        await redis.aclose()

    async with session_factory() as session:
        recovered = await session.get(ExecutionORM, execution.id)
        recovered_attempt = await session.scalar(
            select(ExecutionAttemptORM).where(
                ExecutionAttemptORM.execution_id == execution.id
            )
        )
        failed_events = await session.scalar(
            select(func.count(OutboxEventORM.id)).where(
                OutboxEventORM.aggregate_id == execution.id,
                OutboxEventORM.event_type == "execution.failed",
            )
        )

    assert recovered is not None
    assert recovered.status == ExecutionStatus.FAILED
    assert recovered.failure_type == FailureType.LEASE_EXPIRED
    assert recovered.retryable
    assert recovered.retry_strategy == RetryStrategy.FROM_START
    assert recovered.retry_from_sequence == 0
    assert recovered.recovery_count == 1
    assert recovered.kernel_cleanup_status == KernelCleanupStatus.NOT_REQUIRED
    assert recovered_attempt is not None
    assert recovered_attempt.status == AttemptStatus.FAILED
    assert recovered_attempt.failure_type == FailureType.LEASE_EXPIRED
    assert recovered_attempt.retry_strategy == RetryStrategy.FROM_START
    assert failed_events == 1
