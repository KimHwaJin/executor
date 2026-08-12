import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    ContinueExecutionCommand,
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
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    StepStatus,
    TriggerType,
)
from executor_service.domain.errors import InvalidStateTransitionError
from executor_service.domain.models import Execution, utc_now
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.runtime_registry import RuntimeTargetRegistry
from executor_service.infrastructure.worker import ExecutionWorker


class FakeJupyterGateway:
    session_exists_result = True
    deleted: ClassVar[list[str]] = []
    interrupted: ClassVar[list[str]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def close(self) -> None:
        pass

    async def session_exists(self, _runtime_session_id: str) -> bool:
        return self.session_exists_result

    async def interrupt_session(self, runtime_session_id: str) -> None:
        self.interrupted.append(runtime_session_id)

    async def delete_session(self, runtime_session_id: str) -> None:
        self.deleted.append(runtime_session_id)


def _patch_runtime_driver(monkeypatch: pytest.MonkeyPatch, driver_type: type[Any]) -> None:
    monkeypatch.setattr(
        "executor_service.infrastructure.runtime_drivers.JupyterRuntimeDriver",
        driver_type,
    )


def _dynamic_command(key: str) -> SubmitExecutionCommand:
    code = "value = 1"
    return SubmitExecutionCommand(
        idempotency_key=key,
        mode=ExecutionMode.DYNAMIC,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        code_source_type=CodeSourceType.INLINE,
        source_content=code,
        code_path=None,
        source_sha256="0" * 64,
        user_id="lifecycle-user",
        project_id="lifecycle-project",
        session_id="lifecycle-session",
        task_id="test-task",
        execution_plan_id="lifecycle-plan",
        steps=(
            StepSpec(
                sequence=0,
                code=code,
                execution_plan_id="lifecycle-plan",
                plan_step_id="lifecycle-plan-step-0",
                tool_name="initialize",
            ),
        ),
    )


async def _make_waiting(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    key: str,
    *,
    wait_expired: bool = False,
    execution_expired: bool = False,
    server_enabled: bool = True,
) -> tuple[Execution, ExecutionAttemptORM]:
    execution = await execution_service.submit(_dynamic_command(key))
    now = utc_now()
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        target = RuntimeTargetORM(
            name=f"target-{key}",
            connection_config={"endpoint": "http://fake-jupyter"},
            credential_ref="settings:JUPYTER_TOKEN",
            pool=RuntimePool.INTERACTIVE,
            status=(RuntimeTargetStatus.ACTIVE if server_enabled else RuntimeTargetStatus.OFFLINE),
            max_concurrent_executions=2,
            supported_profiles=["basic"],
            enabled=server_enabled,
        )
        session.add(target)
        await session.flush()
        attempt = ExecutionAttemptORM(
            execution_id=execution.id,
            attempt_number=1,
            runtime_target_id=target.id,
            runtime_session_id=f"kernel-{key}",
            status=AttemptStatus.WAITING,
            lease_owner=None,
            lease_expires_at=None,
            heartbeat_at=now,
            started_at=now - timedelta(minutes=1),
        )
        session.add(attempt)
        await session.flush()
        session.add(
            ExecutionStepAttemptORM(
                execution_id=execution.id,
                execution_attempt_id=attempt.id,
                execution_step_id=execution.steps[0].id,
                sequence=0,
                tool_name="initialize",
                input_parameters={},
                status=StepStatus.SUCCEEDED,
                outputs=[],
                started_at=now - timedelta(minutes=1),
                finished_at=now,
            )
        )
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.WAITING_FOR_NEXT_STEP,
                runtime_target_id=target.id,
                runtime_session_id=attempt.runtime_session_id,
                started_at=now - timedelta(minutes=1),
                dynamic_wait_expires_at=(
                    now - timedelta(seconds=1) if wait_expired else now + timedelta(hours=1)
                ),
                execution_expires_at=(
                    now - timedelta(seconds=1) if execution_expired else now + timedelta(days=1)
                ),
                version=2,
            )
        )
        await session.execute(
            update(ExecutionStepORM)
            .where(ExecutionStepORM.id == execution.steps[0].id)
            .values(status=StepStatus.SUCCEEDED, finished_at=now)
        )
    return execution, attempt


def _worker(engine: AsyncEngine, tmp_path: Path) -> tuple[ExecutionWorker, Redis]:
    settings = Settings(
        runtime_enabled=False,
        workspace_host_root=tmp_path,
        execution_lease_seconds=30,
        execution_heartbeat_seconds=5,
        jupyter_request_timeout_seconds=0.1,
    )
    session_factory = create_session_factory(engine)
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    registry = RuntimeTargetRegistry(session_factory, settings)
    worker = ExecutionWorker(
        session_factory=session_factory,
        redis=redis,
        settings=settings,
        registry=registry,
        artifact_manager=ExecutionArtifactManager(session_factory, settings),
    )
    return worker, redis


@pytest.fixture(autouse=True)
def _reset_fake_gateway() -> None:
    FakeJupyterGateway.session_exists_result = True
    FakeJupyterGateway.deleted = []
    FakeJupyterGateway.interrupted = []


async def test_expired_dynamic_wait_fails_and_cleans_kernel_once(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, _ = await _make_waiting(execution_service, engine, "wait-timeout", wait_expired=True)
    _patch_runtime_driver(monkeypatch, FakeJupyterGateway)
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._audit_dynamic_lifecycle()
        await worker._audit_dynamic_lifecycle()
    finally:
        await redis.aclose()

    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        row = await session.get(ExecutionORM, execution.id)
        attempt = await session.scalar(
            select(ExecutionAttemptORM).where(ExecutionAttemptORM.execution_id == execution.id)
        )
        failed_events = await session.scalar(
            select(func.count(OutboxEventORM.id)).where(
                OutboxEventORM.aggregate_id == execution.id,
                OutboxEventORM.event_type == "execution.failed",
            )
        )
    assert row is not None and attempt is not None
    assert row.status == ExecutionStatus.FAILED
    assert row.failure_type == FailureType.DYNAMIC_WAIT_TIMEOUT
    assert row.runtime_session_id is None
    assert row.runtime_session_cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED
    assert attempt.status == AttemptStatus.FAILED
    assert failed_events == 1
    assert FakeJupyterGateway.deleted == ["kernel-wait-timeout"]
    with pytest.raises(InvalidStateTransitionError):
        await execution_service.continue_execution(
            ContinueExecutionCommand(
                execution_id=execution.id,
                idempotency_key="continue-after-timeout",
                expected_version=row.version,
                step=StepSpec(
                    sequence=1,
                    code="print('too late')",
                    execution_plan_id="lifecycle-plan-2",
                    plan_step_id="lifecycle-plan-2-step-1",
                ),
            )
        )


async def test_restart_audit_detects_missing_kernel_without_cleanup(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, _ = await _make_waiting(execution_service, engine, "kernel-lost")
    FakeJupyterGateway.session_exists_result = False
    _patch_runtime_driver(monkeypatch, FakeJupyterGateway)
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._audit_dynamic_lifecycle()
    finally:
        await redis.aclose()

    async with create_session_factory(engine)() as session:
        row = await session.get(ExecutionORM, execution.id)
    assert row is not None
    assert row.status == ExecutionStatus.FAILED
    assert row.failure_type == FailureType.RUNTIME_SESSION_LOST
    assert row.runtime_session_id is None
    assert row.runtime_session_cleanup_status == RuntimeSessionCleanupStatus.NOT_REQUIRED
    assert FakeJupyterGateway.deleted == []


async def test_disabled_target_fails_waiting_execution(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, _ = await _make_waiting(
        execution_service, engine, "target-disabled", server_enabled=False
    )
    _patch_runtime_driver(monkeypatch, FakeJupyterGateway)
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._audit_dynamic_lifecycle()
    finally:
        await redis.aclose()

    async with create_session_factory(engine)() as session:
        row = await session.get(ExecutionORM, execution.id)
    assert row is not None
    assert row.status == ExecutionStatus.FAILED
    assert row.failure_type == FailureType.RUNTIME_UNAVAILABLE
    assert row.runtime_session_cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED


async def test_execution_deadline_precedes_step_wait_deadline(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, _ = await _make_waiting(
        execution_service,
        engine,
        "execution-timeout",
        wait_expired=True,
        execution_expired=True,
    )
    _patch_runtime_driver(monkeypatch, FakeJupyterGateway)
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._audit_dynamic_lifecycle()
    finally:
        await redis.aclose()

    async with create_session_factory(engine)() as session:
        row = await session.get(ExecutionORM, execution.id)
    assert row is not None
    assert row.status == ExecutionStatus.FAILED
    assert row.failure_type == FailureType.EXECUTION_TIMEOUT


async def test_running_execution_deadline_requests_cancel_and_reclaims_kernel(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution, _ = await _make_waiting(execution_service, engine, "running-timeout")
    now = utc_now()
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.RUNNING,
                execution_expires_at=now - timedelta(seconds=1),
                lease_owner="slow-worker",
                lease_expires_at=now + timedelta(minutes=1),
            )
        )
        await session.execute(
            update(ExecutionAttemptORM)
            .where(ExecutionAttemptORM.execution_id == execution.id)
            .values(
                status=AttemptStatus.RUNNING,
                lease_owner="slow-worker",
                lease_expires_at=now + timedelta(minutes=1),
            )
        )
    _patch_runtime_driver(monkeypatch, FakeJupyterGateway)
    worker, redis = _worker(engine, tmp_path)
    try:
        await worker._audit_dynamic_lifecycle()
        for _ in range(50):
            current = await execution_service.get(execution.id)
            if current.status == ExecutionStatus.CANCELLED:
                break
            await asyncio.sleep(0.01)
        if worker._jobs:
            await asyncio.gather(*list(worker._jobs.values()))
    finally:
        await redis.aclose()

    async with session_factory() as session:
        row = await session.get(ExecutionORM, execution.id)
        timeout_events = await session.scalar(
            select(func.count(OutboxEventORM.id)).where(
                OutboxEventORM.aggregate_id == execution.id,
                OutboxEventORM.event_type == "execution.timeout_requested",
            )
        )
    assert row is not None
    assert row.status == ExecutionStatus.CANCELLED
    assert row.cancellation_reason == "Execution exceeded its maximum runtime."
    assert row.runtime_session_id is None
    assert timeout_events == 1
    assert FakeJupyterGateway.interrupted == ["kernel-running-timeout"]
    assert FakeJupyterGateway.deleted == ["kernel-running-timeout"]
