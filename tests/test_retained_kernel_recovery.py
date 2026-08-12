from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, update
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
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    FailureType,
    RetryStrategy,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import RuntimeDriverError
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    ExecutionStepORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.runtime_registry import RuntimeTargetRegistry
from executor_service.infrastructure.worker import ExecutionWorker


def _command(key: str) -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=key,
        mode=ExecutionMode.STATIC,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="python3",
        code_source_type=CodeSourceType.INLINE,
        source_content="prepare()\nfail_once()",
        code_path=None,
        source_sha256="0" * 64,
        user_id="retained-retry-user",
        project_id="retained-retry-project",
        session_id=f"retained-retry-session-{key}",
        task_id=f"retained-retry-task-{key}",
        execution_plan_id=f"retained-retry-plan-{key}",
        steps=(
            StepSpec(
                0,
                "prepare()",
                f"retained-retry-plan-{key}",
                f"retained-retry-plan-{key}-step-0",
                tool_name="prepare",
            ),
            StepSpec(
                1,
                "fail_once()",
                f"retained-retry-plan-{key}",
                f"retained-retry-plan-{key}-step-1",
                tool_name="fail_once",
            ),
        ),
    )


async def _prepare_retained_retry(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    key: str,
    *,
    server_status: RuntimeTargetStatus,
    server_enabled: bool = True,
    retention_delta: timedelta = timedelta(hours=1),
) -> tuple[ExecutionORM, RuntimeTargetORM]:
    submitted = await execution_service.submit(_command(key))
    now = utc_now()
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        target = RuntimeTargetORM(
            name=f"retained-target-{key}",
            connection_config={"endpoint": "http://retained-jupyter.invalid:8888"},
            credential_ref="settings:JUPYTER_TOKEN",
            pool=RuntimePool.INTERACTIVE,
            status=server_status,
            max_concurrent_executions=2,
            supported_profiles=["python3"],
            enabled=server_enabled,
        )
        session.add(target)
        await session.flush()
        session.add(
            ExecutionAttemptORM(
                execution_id=submitted.id,
                attempt_number=1,
                runtime_target_id=target.id,
                runtime_session_id=f"retained-session-{key}",
                status=AttemptStatus.FAILED,
                heartbeat_at=now,
                error_message="expected tool failure",
                failure_type=FailureType.TOOL_ERROR,
                retry_strategy=RetryStrategy.FROM_FAILED_STEP,
                runtime_session_cleanup_status=RuntimeSessionCleanupStatus.NOT_REQUIRED,
                started_at=now - timedelta(minutes=1),
                finished_at=now,
            )
        )
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == submitted.id)
            .values(
                status=ExecutionStatus.FAILED,
                runtime_target_id=target.id,
                runtime_session_id=f"retained-session-{key}",
                error_message="expected tool failure",
                failure_type=FailureType.TOOL_ERROR,
                retryable=True,
                retry_strategy=RetryStrategy.FROM_FAILED_STEP,
                retry_from_sequence=1,
                retained_runtime_session_until=(
                    now + retention_delta
                    if retention_delta > timedelta(0)
                    else now + timedelta(hours=1)
                ),
                finished_at=now,
            )
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.execution_id == submitted.id)
            .values(status=StepStatus.FAILED, finished_at=now)
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(
                ExecutionStepORM.execution_id == submitted.id,
                ExecutionStepORM.sequence == 0,
            )
            .values(status=StepStatus.SUCCEEDED)
        )

    await execution_service.retry(
        RetryExecutionCommand(
            execution_id=submitted.id,
            idempotency_key=f"retained-retry-{key}",
        )
    )
    if retention_delta <= timedelta(0):
        async with session_factory() as session, session.begin():
            await session.execute(
                update(ExecutionORM)
                .where(ExecutionORM.id == submitted.id)
                .values(retained_runtime_session_until=utc_now() + retention_delta)
            )
    async with session_factory() as session:
        execution = await session.get(ExecutionORM, submitted.id)
        persisted_server = await session.get(RuntimeTargetORM, target.id)
        assert execution is not None
        assert persisted_server is not None
        return execution, persisted_server


def _worker(engine: AsyncEngine, tmp_path: Path) -> tuple[ExecutionWorker, Redis]:
    settings = Settings(runtime_enabled=False, workspace_host_root=tmp_path)
    session_factory = create_session_factory(engine)
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    return (
        ExecutionWorker(
            session_factory=session_factory,
            redis=redis,
            settings=settings,
            registry=RuntimeTargetRegistry(session_factory, settings),
            artifact_manager=ExecutionArtifactManager(session_factory, settings),
        ),
        redis,
    )


async def test_offline_retained_retry_waits_then_claims_the_same_server_and_kernel(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    execution, target = await _prepare_retained_retry(
        execution_service,
        engine,
        "offline-recovery",
        server_status=RuntimeTargetStatus.OFFLINE,
    )
    worker, redis = _worker(engine, tmp_path)
    session_factory = create_session_factory(engine)
    try:
        assert await worker._claim(execution.id) is None
        capacity_view = await worker._registry.get(target.id)
        assert capacity_view.active_execution_count == 1
        async with session_factory() as session:
            waiting = await session.get(ExecutionORM, execution.id)
            assert waiting is not None
            assert waiting.status == ExecutionStatus.QUEUED
            assert waiting.retry_strategy == RetryStrategy.FROM_FAILED_STEP
            assert waiting.runtime_target_id == target.id
            assert waiting.runtime_session_id == "retained-session-offline-recovery"
            attempt_count = len(
                list(
                    await session.scalars(
                        select(ExecutionAttemptORM).where(
                            ExecutionAttemptORM.execution_id == execution.id
                        )
                    )
                )
            )
            assert attempt_count == 1

        async with session_factory() as session, session.begin():
            await session.execute(
                update(RuntimeTargetORM)
                .where(RuntimeTargetORM.id == target.id)
                .values(status=RuntimeTargetStatus.ACTIVE)
            )

        claimed = await worker._claim(execution.id)
        assert claimed is not None
        claimed_execution, claimed_server, _attempt_id = claimed
        assert claimed_server.id == target.id
        assert claimed_execution.runtime_session_id == "retained-session-offline-recovery"
        assert claimed_execution.retry_strategy == RetryStrategy.FROM_FAILED_STEP
    finally:
        await redis.aclose()


async def test_disabled_retained_server_requires_an_explicit_from_start_retry(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    execution, target = await _prepare_retained_retry(
        execution_service,
        engine,
        "disabled-target",
        server_status=RuntimeTargetStatus.OFFLINE,
        server_enabled=False,
    )
    worker, redis = _worker(engine, tmp_path)
    session_factory = create_session_factory(engine)
    try:
        assert await worker._claim(execution.id) is None
    finally:
        await redis.aclose()

    async with session_factory() as session:
        failed = await session.get(ExecutionORM, execution.id)
        assert failed is not None
        assert failed.status == ExecutionStatus.FAILED
        assert failed.failure_type == FailureType.RUNTIME_UNAVAILABLE
        assert failed.retryable
        assert failed.retry_strategy == RetryStrategy.FROM_START
        assert failed.retry_from_sequence == 0
        assert failed.runtime_target_id == target.id
        event = await session.scalar(
            select(OutboxEventORM).where(
                OutboxEventORM.aggregate_id == execution.id,
                OutboxEventORM.event_type == "execution.failed",
            )
        )
        assert event is not None
        assert event.payload["reason"] == "retained_target_unavailable"


async def test_draining_server_allows_its_retained_runtime_session_to_finish(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    execution, target = await _prepare_retained_retry(
        execution_service,
        engine,
        "draining-target",
        server_status=RuntimeTargetStatus.DRAINING,
    )
    worker, redis = _worker(engine, tmp_path)
    try:
        claimed = await worker._claim(execution.id)
    finally:
        await redis.aclose()

    assert claimed is not None
    claimed_execution, claimed_server, _attempt_id = claimed
    assert claimed_server.id == target.id
    assert claimed_server.status == RuntimeTargetStatus.DRAINING
    assert claimed_execution.runtime_session_id == "retained-session-draining-target"
    assert claimed_execution.retry_strategy == RetryStrategy.FROM_FAILED_STEP


class MissingKernelGateway:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def session_exists(self, _runtime_session_id: str) -> bool:
        return False

    async def interrupt_session(self, _runtime_session_id: str) -> None:
        pass

    async def delete_session(self, _runtime_session_id: str) -> None:
        pass

    async def close(self) -> None:
        pass


class UnavailableKernelGateway:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def session_exists(self, _runtime_session_id: str) -> bool:
        raise RuntimeDriverError("temporary preflight outage")

    async def close(self) -> None:
        pass


def _patch_runtime_driver(monkeypatch: pytest.MonkeyPatch, driver_type: type[Any]) -> None:
    monkeypatch.setattr(
        "executor_service.infrastructure.runtime_drivers.JupyterRuntimeDriver",
        driver_type,
    )


async def test_preflight_connection_failure_defers_the_retained_retry(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, target = await _prepare_retained_retry(
        execution_service,
        engine,
        "preflight-outage",
        server_status=RuntimeTargetStatus.ACTIVE,
    )
    worker, redis = _worker(engine, tmp_path)
    _patch_runtime_driver(monkeypatch, UnavailableKernelGateway)
    try:
        await worker._run_execution(execution.id)
    finally:
        await redis.aclose()

    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        deferred = await session.get(ExecutionORM, execution.id)
        persisted_server = await session.get(RuntimeTargetORM, target.id)
        attempts = list(
            await session.scalars(
                select(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.execution_id == execution.id)
                .order_by(ExecutionAttemptORM.attempt_number)
            )
        )
        event = await session.scalar(
            select(OutboxEventORM).where(
                OutboxEventORM.aggregate_id == execution.id,
                OutboxEventORM.event_type == "execution.retry_deferred",
            )
        )
        assert deferred is not None
        assert deferred.status == ExecutionStatus.QUEUED
        assert deferred.retryable
        assert deferred.retry_strategy == RetryStrategy.FROM_FAILED_STEP
        assert deferred.runtime_target_id == target.id
        assert deferred.runtime_session_id == "retained-session-preflight-outage"
        assert deferred.retained_runtime_session_until is not None
        assert persisted_server is not None
        assert persisted_server.status == RuntimeTargetStatus.OFFLINE
        assert len(attempts) == 2
        assert attempts[-1].status == AttemptStatus.FAILED
        assert attempts[-1].failure_type == FailureType.RUNTIME_UNAVAILABLE
        assert attempts[-1].retry_strategy == RetryStrategy.FROM_FAILED_STEP
        assert event is not None
        assert event.payload["reason"] == "retained_target_temporarily_unavailable"


async def test_missing_retained_runtime_session_fails_without_running_on_another_server(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, target = await _prepare_retained_retry(
        execution_service,
        engine,
        "missing-kernel",
        server_status=RuntimeTargetStatus.ACTIVE,
    )
    worker, redis = _worker(engine, tmp_path)
    _patch_runtime_driver(monkeypatch, MissingKernelGateway)
    try:
        await worker._run_execution(execution.id)
    finally:
        await redis.aclose()

    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        failed = await session.get(ExecutionORM, execution.id)
        attempts = list(
            await session.scalars(
                select(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.execution_id == execution.id)
                .order_by(ExecutionAttemptORM.attempt_number)
            )
        )
        assert failed is not None
        assert failed.status == ExecutionStatus.FAILED
        assert failed.failure_type == FailureType.RUNTIME_SESSION_LOST
        assert failed.retryable
        assert failed.retry_strategy == RetryStrategy.FROM_START
        assert failed.runtime_session_id is None
        assert failed.runtime_target_id == target.id
        assert len(attempts) == 2
        assert attempts[-1].failure_type == FailureType.RUNTIME_SESSION_LOST
        assert attempts[-1].runtime_target_id == target.id


class CleanupGateway:
    deleted: ClassVar[list[str]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def delete_session(self, runtime_session_id: str) -> None:
        self.deleted.append(runtime_session_id)

    async def close(self) -> None:
        pass


async def test_queued_retained_retry_expires_without_switching_servers(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, _server = await _prepare_retained_retry(
        execution_service,
        engine,
        "expired-queued",
        server_status=RuntimeTargetStatus.OFFLINE,
        retention_delta=timedelta(seconds=-1),
    )
    worker, redis = _worker(engine, tmp_path)
    CleanupGateway.deleted = []
    _patch_runtime_driver(monkeypatch, CleanupGateway)
    try:
        await worker._cleanup_expired_retained_runtime_sessions()
    finally:
        await redis.aclose()

    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        expired = await session.get(ExecutionORM, execution.id)
        assert expired is not None
        assert expired.status == ExecutionStatus.FAILED
        assert not expired.retryable
        assert expired.retry_strategy == RetryStrategy.NOT_RETRYABLE
        assert expired.runtime_session_id is None
        assert CleanupGateway.deleted == ["retained-session-expired-queued"]
        event = await session.scalar(
            select(OutboxEventORM).where(
                OutboxEventORM.aggregate_id == execution.id,
                OutboxEventORM.event_type == "execution.retry_window_expired",
            )
        )
        assert event is not None
        assert event.payload["retry_was_queued"] is True
