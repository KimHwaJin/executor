from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    RetryExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    FailureType,
    OperationMode,
    OperationStatus,
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    StepStatus,
    TriggerType,
)
from executor_service.domain.errors import InvalidStateTransitionError
from executor_service.domain.models import utc_now
from executor_service.domain.results import StepResultDescriptor
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionEventORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_leases import (
    ExecutionLease,
    ExecutionLeaseLostError,
)
from executor_service.infrastructure.execution_worker import ExecutionWorker
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from tests.runtime_credentials import runtime_credential_fields


class RecoveryCleanupDriver:
    delete_fails: ClassVar[bool] = False
    deleted: ClassVar[list[str]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)
        if self.delete_fails:
            raise RuntimeError("expected cleanup failure")

    async def close(self) -> None:
        pass


def _command() -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key="lease-recovery-submit",
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="recovery-user",
        project_id="recovery-project",
        session_id="recovery-session",
        task_id="test-task",
        steps=(
            StepSpec(
                sequence=0,
                code="print('long-running')",
                tool_name="long_running_tool",
            ),
        ),
    )


async def test_expired_lease_is_failed_once_and_can_restart_from_zero(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    execution = await execution_service.submit(_command())
    now = utc_now()
    fencing_token = 7
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        target = RuntimeTargetORM(
            name="recovery-jupyter",
            connection_config={"endpoint": "http://127.0.0.1:9"},
            **runtime_credential_fields(),
            pool=RuntimePool.INTERACTIVE,
            status=RuntimeTargetStatus.ACTIVE,
            max_concurrent_executions=1,
            supported_profiles=["basic"],
            enabled=True,
        )
        session.add(target)
        await session.flush()
        attempt = ExecutionAttemptORM(
            execution_id=execution.id,
            attempt_number=1,
            runtime_target_id=target.id,
            status=AttemptStatus.RUNNING,
            lease_owner="dead-worker",
            lease_expires_at=now - timedelta(seconds=1),
            heartbeat_at=now - timedelta(minutes=1),
            fencing_token=fencing_token,
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
                started_at=now - timedelta(minutes=2),
            )
        )
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.RUNNING,
                runtime_target_id=target.id,
                runtime_session_id=None,
                lease_owner="dead-worker",
                lease_expires_at=now - timedelta(seconds=1),
                heartbeat_at=now - timedelta(minutes=1),
                fencing_token=fencing_token,
                started_at=now - timedelta(minutes=2),
            )
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.id == execution.steps[0].id)
            .values(
                status=StepStatus.RUNNING,
                started_at=now - timedelta(minutes=2),
            )
        )
        await session.execute(
            update(ExecutionOperationORM)
            .where(ExecutionOperationORM.id == execution.active_operation_id)
            .values(
                status=OperationStatus.RUNNING,
                execution_attempt_id=attempt.id,
                started_at=now - timedelta(minutes=2),
            )
        )

    settings = Settings(
        runtime_enabled=False,
        shared_storage_root=tmp_path,
        execution_lease_seconds=30,
        execution_heartbeat_seconds=5,
    )
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    registry = RuntimeTargetRegistry(session_factory, settings)
    worker = ExecutionWorker(
        session_factory=session_factory,
        redis=redis,
        settings=settings,
        registry=registry,
        artifact_manager=ExecutionArtifactManager(session_factory),
    )
    stale_lease = ExecutionLease(
        execution_id=execution.id,
        attempt_id=attempt.id,
        owner="dead-worker",
        fencing_token=fencing_token,
    )
    try:
        await worker._lease_recovery.recover()
        await worker._lease_recovery.recover()
        with pytest.raises(ExecutionLeaseLostError):
            await worker._step_executor.mark_succeeded(
                stale_lease,
                0,
                cast(StepResultDescriptor, None),
            )
        with pytest.raises(ExecutionLeaseLostError):
            await worker._runner._finalizer.finalize(
                stale_lease,
                ExecutionStatus.SUCCEEDED,
            )
    finally:
        await redis.aclose()

    async with session_factory() as session:
        recovered = await session.get(ExecutionORM, execution.id)
        recovered_attempt = await session.scalar(
            select(ExecutionAttemptORM).where(
                ExecutionAttemptORM.execution_id == execution.id
            )
        )
        recovered_operation = await session.get(
            ExecutionOperationORM, execution.active_operation_id
        )
        failed_events = await session.scalar(
            select(func.count(ExecutionEventORM.id)).where(
                ExecutionEventORM.execution_id == execution.id,
                ExecutionEventORM.event_type == "execution.completed",
                ExecutionEventORM.payload["status"].as_string() == "FAILED",
            )
        )
        operation_failed_events = await session.scalar(
            select(func.count(ExecutionEventORM.id)).where(
                ExecutionEventORM.execution_id == execution.id,
                ExecutionEventORM.event_type
                == "execution.operation_completed",
                ExecutionEventORM.payload["status"].as_string() == "FAILED",
            )
        )
        stale_step_events = await session.scalar(
            select(func.count(ExecutionEventORM.id)).where(
                ExecutionEventORM.execution_id == execution.id,
                ExecutionEventORM.event_type == "execution.step_completed",
                ExecutionEventORM.payload["status"].as_string() == "SUCCEEDED",
            )
        )
        stale_terminal_events = await session.scalar(
            select(func.count(ExecutionEventORM.id)).where(
                ExecutionEventORM.execution_id == execution.id,
                ExecutionEventORM.event_type == "execution.completed",
                ExecutionEventORM.payload["status"].as_string() == "SUCCEEDED",
            )
        )

    assert recovered is not None
    assert recovered.status == ExecutionStatus.FAILED
    assert recovered.failure_type == FailureType.LEASE_EXPIRED
    assert recovered.retry_strategy != RetryStrategy.NOT_RETRYABLE
    assert recovered.retry_strategy == RetryStrategy.FROM_START
    assert recovered.retry_from_sequence == 0
    assert recovered.recovery_count == 1
    assert recovered.fencing_token == fencing_token + 1
    assert (
        recovered.runtime_session_cleanup_status
        == RuntimeSessionCleanupStatus.NOT_REQUIRED
    )
    assert recovered_attempt is not None
    assert recovered_attempt.status == AttemptStatus.FAILED
    assert recovered_attempt.failure_type == FailureType.LEASE_EXPIRED
    assert recovered_attempt.retry_strategy == RetryStrategy.FROM_START
    assert recovered_attempt.fencing_token == fencing_token
    assert recovered_operation is not None
    assert recovered_operation.status == OperationStatus.FAILED
    assert recovered_operation.execution_attempt_id == recovered_attempt.id
    assert operation_failed_events == 1
    assert failed_events == 1
    assert stale_step_events == 0
    assert stale_terminal_events == 0


async def test_orphan_running_execution_without_attempt_is_fenced_and_cleaned(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = await execution_service.submit(
        replace(
            _command(),
            idempotency_key="orphan-without-attempt",
            session_id="orphan-without-attempt",
        )
    )
    now = utc_now()
    runtime_session_id = "orphan-session-without-attempt"
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        target = RuntimeTargetORM(
            name="orphan-without-attempt-target",
            connection_config={"endpoint": "http://recovery.invalid"},
            **runtime_credential_fields(),
            pool=RuntimePool.INTERACTIVE,
            status=RuntimeTargetStatus.ACTIVE,
            max_concurrent_executions=1,
            supported_profiles=["basic"],
            enabled=True,
        )
        session.add(target)
        await session.flush()
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.RUNNING,
                runtime_target_id=target.id,
                runtime_session_id=runtime_session_id,
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=None,
                fencing_token=3,
                started_at=now - timedelta(minutes=2),
            )
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.id == execution.steps[0].id)
            .values(
                status=StepStatus.RUNNING,
                started_at=now - timedelta(minutes=2),
            )
        )
        await session.execute(
            update(ExecutionOperationORM)
            .where(ExecutionOperationORM.id == execution.active_operation_id)
            .values(
                status=OperationStatus.RUNNING,
                started_at=now - timedelta(minutes=2),
            )
        )

    RecoveryCleanupDriver.delete_fails = False
    RecoveryCleanupDriver.deleted = []
    monkeypatch.setattr(
        "executor_service.infrastructure.runtime_drivers.JupyterRuntimeDriver",
        RecoveryCleanupDriver,
    )
    settings = Settings(
        runtime_enabled=False,
        shared_storage_root=tmp_path,
    )
    redis = Redis.from_url(
        "redis://127.0.0.1:6379/15",
        decode_responses=True,
    )
    worker = ExecutionWorker(
        session_factory=session_factory,
        redis=redis,
        settings=settings,
        registry=RuntimeTargetRegistry(session_factory, settings),
        artifact_manager=ExecutionArtifactManager(session_factory),
    )
    try:
        recovered_count = await worker._lease_recovery.recover()
    finally:
        await redis.aclose()

    async with session_factory() as session:
        recovered = await session.get(ExecutionORM, execution.id)
        completed_events = await session.scalar(
            select(func.count(ExecutionEventORM.id)).where(
                ExecutionEventORM.execution_id == execution.id,
                ExecutionEventORM.event_type == "execution.completed",
            )
        )

    assert recovered_count == 1
    assert recovered is not None
    assert recovered.status == ExecutionStatus.FAILED
    assert recovered.failure_type == FailureType.LEASE_EXPIRED
    assert recovered.fencing_token == 4
    assert recovered.runtime_session_id is None
    assert (
        recovered.runtime_session_cleanup_status
        == RuntimeSessionCleanupStatus.SUCCEEDED
    )
    assert RecoveryCleanupDriver.deleted == [runtime_session_id]
    assert completed_events == 1


@pytest.mark.parametrize("delete_fails", [False, True])
async def test_expired_lease_resolves_pending_runtime_abort(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delete_fails: bool,
) -> None:
    execution = await execution_service.submit(
        replace(
            _command(),
            idempotency_key=f"abort-recovery-{delete_fails}",
            session_id=f"abort-recovery-{delete_fails}",
        )
    )
    now = utc_now()
    fencing_token = 11
    runtime_session_id = f"pending-abort-{delete_fails}"
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        target = RuntimeTargetORM(
            name=f"abort-recovery-target-{delete_fails}",
            connection_config={"endpoint": "http://recovery.invalid"},
            **runtime_credential_fields(),
            pool=RuntimePool.INTERACTIVE,
            status=RuntimeTargetStatus.ACTIVE,
            max_concurrent_executions=1,
            supported_profiles=["basic"],
            enabled=True,
        )
        session.add(target)
        await session.flush()
        attempt = ExecutionAttemptORM(
            execution_id=execution.id,
            attempt_number=1,
            runtime_target_id=target.id,
            runtime_session_id=runtime_session_id,
            status=AttemptStatus.RUNNING,
            lease_owner="dead-abort-worker",
            lease_expires_at=now - timedelta(seconds=1),
            heartbeat_at=now - timedelta(minutes=1),
            fencing_token=fencing_token,
            failure_type=FailureType.STEP_TIMEOUT,
            runtime_abort_status=RuntimeAbortStatus.PENDING,
            runtime_session_cleanup_status=(
                RuntimeSessionCleanupStatus.PENDING
            ),
            started_at=now - timedelta(minutes=2),
        )
        session.add(attempt)
        await session.flush()
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.RUNNING,
                runtime_target_id=target.id,
                runtime_session_id=runtime_session_id,
                lease_owner="dead-abort-worker",
                lease_expires_at=now - timedelta(seconds=1),
                heartbeat_at=now - timedelta(minutes=1),
                fencing_token=fencing_token,
                failure_type=FailureType.STEP_TIMEOUT,
                runtime_abort_status=RuntimeAbortStatus.PENDING,
                runtime_session_cleanup_status=(
                    RuntimeSessionCleanupStatus.PENDING
                ),
                started_at=now - timedelta(minutes=2),
            )
        )
        await session.execute(
            update(ExecutionOperationORM)
            .where(ExecutionOperationORM.id == execution.active_operation_id)
            .values(
                status=OperationStatus.RUNNING,
                execution_attempt_id=attempt.id,
                started_at=now - timedelta(minutes=2),
            )
        )

    RecoveryCleanupDriver.delete_fails = delete_fails
    RecoveryCleanupDriver.deleted = []
    monkeypatch.setattr(
        "executor_service.infrastructure.runtime_drivers.JupyterRuntimeDriver",
        RecoveryCleanupDriver,
    )
    settings = Settings(
        runtime_enabled=False,
        shared_storage_root=tmp_path,
        execution_lease_seconds=30,
        execution_heartbeat_seconds=5,
    )
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    worker = ExecutionWorker(
        session_factory=session_factory,
        redis=redis,
        settings=settings,
        registry=RuntimeTargetRegistry(session_factory, settings),
        artifact_manager=ExecutionArtifactManager(session_factory),
    )
    try:
        await worker._lease_recovery.recover()
    finally:
        await redis.aclose()

    expected_abort_status = (
        RuntimeAbortStatus.FAILED
        if delete_fails
        else RuntimeAbortStatus.SESSION_DELETED
    )
    expected_cleanup_status = (
        RuntimeSessionCleanupStatus.FAILED
        if delete_fails
        else RuntimeSessionCleanupStatus.SUCCEEDED
    )
    async with session_factory() as session:
        row = await session.get(ExecutionORM, execution.id)
        attempt_row = await session.get(ExecutionAttemptORM, attempt.id)
        abort_events = list(
            await session.scalars(
                select(ExecutionEventORM).where(
                    ExecutionEventORM.execution_id == execution.id,
                    ExecutionEventORM.event_type.like(
                        "execution.runtime_abort_%"
                    ),
                )
            )
        )
    assert row is not None and attempt_row is not None
    assert row.status == ExecutionStatus.FAILED
    assert row.failure_type == FailureType.STEP_TIMEOUT
    assert row.retry_strategy == RetryStrategy.FROM_START
    assert row.runtime_abort_status == expected_abort_status
    assert attempt_row.runtime_abort_status == expected_abort_status
    assert row.runtime_session_cleanup_status == expected_cleanup_status
    assert (row.runtime_session_id is not None) is delete_fails
    assert RecoveryCleanupDriver.deleted == [runtime_session_id]
    assert abort_events == []

    if delete_fails:
        with pytest.raises(
            InvalidStateTransitionError,
            match="unresolved abandoned Runtime session cleanup",
        ):
            await execution_service.retry(
                RetryExecutionCommand(
                    execution_id=execution.id,
                    idempotency_key=f"blocked-abort-retry-{execution.id}",
                )
            )
