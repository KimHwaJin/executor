import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    CancelExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import (
    ExecutionStatus,
    OperationMode,
    RuntimePool,
    RuntimeTargetStatus,
    TriggerType,
)
from executor_service.domain.runtime import RuntimeExecutionResult
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import RuntimeTargetORM
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.runtime_registry import RuntimeTargetRegistry
from executor_service.infrastructure.worker import ExecutionWorker
from tests.runtime_credentials import runtime_credential_fields
from tests.runtime_storage_fake import InMemoryRuntimeStorage


class ControlledJupyterGateway(InMemoryRuntimeStorage):
    blocked_pool = RuntimePool.BATCH
    release = asyncio.Event()
    independent_finished = asyncio.Event()
    blocked_started = 0

    def __init__(self, endpoint: str, *_args: Any, **_kwargs: Any) -> None:
        self.pool = RuntimePool.BATCH if "batch" in endpoint else RuntimePool.INTERACTIVE

    @classmethod
    def configure(cls, blocked_pool: RuntimePool) -> None:
        cls.reset_storage()
        cls.blocked_pool = blocked_pool
        cls.release = asyncio.Event()
        cls.independent_finished = asyncio.Event()
        cls.blocked_started = 0

    async def start_session(self, _runtime_profile: str, _path: str) -> str:
        return f"kernel-{uuid4().hex}"

    async def execute(self, _runtime_session_id: str, _code: str) -> RuntimeExecutionResult:
        if self.pool == self.blocked_pool:
            type(self).blocked_started += 1
            await self.release.wait()
        else:
            self.independent_finished.set()
        return RuntimeExecutionResult(outputs=[], execution_count=1)

    async def interrupt_session(self, _runtime_session_id: str) -> None:
        pass

    async def delete_session(self, _runtime_session_id: str) -> None:
        pass

    async def close(self) -> None:
        pass


def _patch_runtime_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "executor_service.infrastructure.runtime_drivers.JupyterRuntimeDriver",
        ControlledJupyterGateway,
    )


def _command(pool: RuntimePool, name: str) -> SubmitExecutionCommand:
    code = f"value = '{name}'"
    return SubmitExecutionCommand(
        idempotency_key=f"worker-pool-{name}-{uuid4().hex}",
        operation_mode=OperationMode.SINGLE,
        trigger_type=(TriggerType.BATCH if pool == RuntimePool.BATCH else TriggerType.INTERACTIVE),
        runtime_profile="basic",
        user_id="worker-pool-user",
        project_id="worker-pool-project",
        session_id=f"worker-pool-session-{name}",
        task_id="test-task",
        workflow_id=f"worker-pool-workflow-{name}" if pool == RuntimePool.BATCH else None,
        steps=(
            StepSpec(
                sequence=0,
                code=code,
                tool_name=name,
            ),
        ),
    )


def _target(name: str, pool: RuntimePool, *, capacity: int = 10) -> RuntimeTargetORM:
    return RuntimeTargetORM(
        name=name,
        connection_config={"endpoint": f"http://{name}.invalid:8888"},
        **runtime_credential_fields(),
        pool=pool,
        status=RuntimeTargetStatus.ACTIVE,
        max_concurrent_executions=capacity,
        supported_profiles=["basic"],
        enabled=True,
    )


def _worker(engine: AsyncEngine, tmp_path: Path) -> tuple[ExecutionWorker, Redis]:
    settings = Settings(
        runtime_enabled=False,
        input_host_root=tmp_path,
    )
    session_factory = create_session_factory(engine)
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    return (
        ExecutionWorker(
            session_factory=session_factory,
            redis=redis,
            settings=settings,
            registry=RuntimeTargetRegistry(session_factory, settings),
            artifact_manager=ExecutionArtifactManager(session_factory),
        ),
        redis,
    )


async def _wait_until(predicate: Any) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Condition was not met in time")


@pytest.mark.parametrize(
    ("blocked_pool", "independent_pool", "blocked_count"),
    [
        (RuntimePool.BATCH, RuntimePool.INTERACTIVE, 3),
        (RuntimePool.INTERACTIVE, RuntimePool.BATCH, 2),
    ],
)
async def test_worker_does_not_apply_a_process_local_execution_limit(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_pool: RuntimePool,
    independent_pool: RuntimePool,
    blocked_count: int,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                _target("worker-interactive", RuntimePool.INTERACTIVE),
                _target("worker-batch", RuntimePool.BATCH),
            ]
        )
    blocked = [
        await execution_service.submit(
            _command(blocked_pool, f"blocked-{index}-{blocked_pool.value.lower()}")
        )
        for index in range(blocked_count)
    ]
    independent = await execution_service.submit(
        _command(independent_pool, f"independent-{independent_pool.value.lower()}")
    )
    worker, redis = _worker(engine, tmp_path)
    ControlledJupyterGateway.configure(blocked_pool)
    _patch_runtime_driver(monkeypatch)
    blocked_tasks = [
        asyncio.create_task(worker._run_execution(execution.id)) for execution in blocked
    ]
    try:
        await _wait_until(lambda: ControlledJupyterGateway.blocked_started == blocked_count)
        independent_task = asyncio.create_task(worker._run_execution(independent.id))
        async with asyncio.timeout(1):
            await ControlledJupyterGateway.independent_finished.wait()
            await independent_task

    finally:
        ControlledJupyterGateway.release.set()
        await asyncio.gather(*blocked_tasks)
        await redis.aclose()


async def test_cancel_remains_available_when_batch_runtime_capacity_is_full(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(_target("cancel-batch", RuntimePool.BATCH, capacity=2))
    running = [
        await execution_service.submit(_command(RuntimePool.BATCH, f"cancel-running-{index}"))
        for index in range(2)
    ]
    waiting = await execution_service.submit(_command(RuntimePool.BATCH, "cancel-waiting"))
    worker, redis = _worker(engine, tmp_path)
    try:
        assert await worker._claim(running[0].id) is not None
        assert await worker._claim(running[1].id) is not None
        assert await worker._claim(waiting.id) is None
        assert (await execution_service.get(waiting.id)).status == ExecutionStatus.QUEUED
        await execution_service.cancel(
            CancelExecutionCommand(
                execution_id=waiting.id,
                idempotency_key=f"cancel-waiting-{uuid4().hex}",
                reason="verify cancellation bypass",
            )
        )
        await worker._cancel_execution(waiting.id)
    finally:
        await redis.aclose()

    assert (await execution_service.get(waiting.id)).status == ExecutionStatus.CANCELLED
