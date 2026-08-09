import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from prometheus_client import generate_latest
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
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    JupyterPool,
    JupyterServerStatus,
    TriggerType,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import JupyterServerORM
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.jupyter import CellExecutionResult
from executor_service.infrastructure.jupyter_registry import JupyterServerRegistry
from executor_service.infrastructure.worker import ExecutionWorker


class ControlledJupyterGateway:
    blocked_pool = JupyterPool.BATCH
    release = asyncio.Event()
    independent_finished = asyncio.Event()
    blocked_started = 0

    def __init__(self, endpoint: str, *_args: Any, **_kwargs: Any) -> None:
        self.pool = (
            JupyterPool.BATCH if "batch" in endpoint else JupyterPool.INTERACTIVE
        )

    @classmethod
    def configure(cls, blocked_pool: JupyterPool) -> None:
        cls.blocked_pool = blocked_pool
        cls.release = asyncio.Event()
        cls.independent_finished = asyncio.Event()
        cls.blocked_started = 0

    async def start_kernel(self, _kernel_name: str, _path: str) -> str:
        return f"kernel-{uuid4().hex}"

    async def execute_cell(self, _kernel_id: str, _code: str) -> CellExecutionResult:
        if self.pool == self.blocked_pool:
            type(self).blocked_started += 1
            await self.release.wait()
        else:
            self.independent_finished.set()
        return CellExecutionResult(outputs=[], execution_count=1)

    async def interrupt_kernel(self, _kernel_id: str) -> None:
        pass

    async def delete_kernel(self, _kernel_id: str) -> None:
        pass

    async def close(self) -> None:
        pass


def _command(pool: JupyterPool, name: str) -> SubmitExecutionCommand:
    code = f"value = '{name}'"
    return SubmitExecutionCommand(
        idempotency_key=f"worker-pool-{name}-{uuid4().hex}",
        mode=ExecutionMode.STATIC,
        trigger_type=(
            TriggerType.BATCH if pool == JupyterPool.BATCH else TriggerType.INTERACTIVE
        ),
        jupyter_pool=pool,
        kernel_name="python3",
        code_source_type=CodeSourceType.INLINE,
        code=code,
        code_path=None,
        requested_by_user_id="worker-pool-user",
        project_id="worker-pool-project",
        session_id=f"worker-pool-session-{name}",
        execution_plan_id=f"worker-pool-plan-{name}",
        steps=(StepSpec(sequence=0, tool_name=name),),
    )


def _server(
    name: str, pool: JupyterPool, *, capacity: int = 10
) -> JupyterServerORM:
    return JupyterServerORM(
        name=name,
        endpoint=f"http://{name}.invalid:8888",
        credential_ref="settings:JUPYTER_TOKEN",
        pool=pool,
        status=JupyterServerStatus.ACTIVE,
        max_concurrent_executions=capacity,
        supported_kernels=["python3"],
        enabled=True,
    )


def _worker(engine: AsyncEngine, tmp_path: Path) -> tuple[ExecutionWorker, Redis]:
    settings = Settings(
        jupyter_enabled=False,
        workspace_host_root=tmp_path,
    )
    session_factory = create_session_factory(engine)
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    return (
        ExecutionWorker(
            session_factory=session_factory,
            redis=redis,
            settings=settings,
            registry=JupyterServerRegistry(session_factory, settings),
            artifact_manager=ExecutionArtifactManager(session_factory, settings),
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
        (JupyterPool.BATCH, JupyterPool.INTERACTIVE, 3),
        (JupyterPool.INTERACTIVE, JupyterPool.BATCH, 2),
    ],
)
async def test_worker_does_not_apply_a_process_local_execution_limit(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_pool: JupyterPool,
    independent_pool: JupyterPool,
    blocked_count: int,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                _server("worker-interactive", JupyterPool.INTERACTIVE),
                _server("worker-batch", JupyterPool.BATCH),
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
    monkeypatch.setattr(
        "executor_service.infrastructure.worker.JupyterGateway",
        ControlledJupyterGateway,
    )
    blocked_tasks = [
        asyncio.create_task(worker._run_execution(execution.id)) for execution in blocked
    ]
    try:
        await _wait_until(
            lambda: ControlledJupyterGateway.blocked_started == blocked_count
        )
        independent_task = asyncio.create_task(worker._run_execution(independent.id))
        async with asyncio.timeout(1):
            await ControlledJupyterGateway.independent_finished.wait()
            await independent_task

        metrics = generate_latest().decode()
        assert (
            f'executor_worker_pool_active_jobs{{pool="{blocked_pool.value}"}} '
            f"{float(blocked_count)}"
        ) in metrics
    finally:
        ControlledJupyterGateway.release.set()
        await asyncio.gather(*blocked_tasks)
        await redis.aclose()

    metrics = generate_latest().decode()
    assert (
        f'executor_worker_pool_active_jobs{{pool="{blocked_pool.value}"}} 0.0'
        in metrics
    )


async def test_cancel_remains_available_when_batch_jupyter_capacity_is_full(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(_server("cancel-batch", JupyterPool.BATCH, capacity=2))
    running = [
        await execution_service.submit(_command(JupyterPool.BATCH, f"cancel-running-{index}"))
        for index in range(2)
    ]
    waiting = await execution_service.submit(_command(JupyterPool.BATCH, "cancel-waiting"))
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
