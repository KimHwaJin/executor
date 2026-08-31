import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.config import Settings
from executor_service.container import ApplicationContainer
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_worker import (
    ExecutionWorker,
    ExpiredLeaseRecovery,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)


class IdleRedis:
    async def xgroup_create(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def xreadgroup(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        await asyncio.Event().wait()
        return []

    async def xautoclaim(
        self, *_args: Any, **_kwargs: Any
    ) -> tuple[str, list[Any]]:
        return "0-0", []


def _worker(
    engine: AsyncEngine,
    tmp_path: Path,
    *,
    drain_timeout: float = 1,
) -> ExecutionWorker:
    settings = Settings(
        runtime_enabled=True,
        shared_storage_root=tmp_path,
        execution_drain_timeout_seconds=drain_timeout,
        execution_pending_claim_interval_seconds=60,
    )
    session_factory = create_session_factory(engine)
    return ExecutionWorker(
        session_factory=session_factory,
        redis=cast(Redis, IdleRedis()),
        settings=settings,
        registry=RuntimeTargetRegistry(session_factory, settings),
        artifact_manager=ExecutionArtifactManager(session_factory),
    )


async def test_stop_drains_active_job_and_rejects_new_dispatch(
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    worker = _worker(engine, tmp_path)
    await worker.start()
    release = asyncio.Event()
    active_finished = asyncio.Event()
    rejected_started = asyncio.Event()

    async def active_job() -> None:
        await release.wait()
        active_finished.set()

    async def rejected_job() -> None:
        rejected_started.set()

    worker._dispatcher.dispatch(uuid4(), active_job())
    await asyncio.sleep(0)
    stop_task = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)

    assert worker.lifecycle_state == "DRAINING"
    assert not worker.accepting_work
    assert worker.active_job_count == 1
    worker._dispatcher.dispatch(uuid4(), rejected_job())
    await asyncio.sleep(0)
    assert not rejected_started.is_set()
    assert not stop_task.done()

    release.set()
    await stop_task

    assert active_finished.is_set()
    assert worker.lifecycle_state == "STOPPED"
    assert worker.active_job_count == 0


async def test_startup_reconciliation_blocks_admission_until_fencing_finishes(
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(engine, tmp_path)
    reconciliation_started = asyncio.Event()
    release_reconciliation = asyncio.Event()

    async def reconcile() -> ExpiredLeaseRecovery:
        reconciliation_started.set()
        await release_reconciliation.wait()
        return ExpiredLeaseRecovery(execution_count=2, cleanup_targets=())

    monkeypatch.setattr(
        worker._lease_recovery,
        "fence_expired_leases",
        reconcile,
    )
    start_task = asyncio.create_task(worker.start())
    await reconciliation_started.wait()

    assert worker.lifecycle_state == "STARTING"
    assert not worker.accepting_work
    assert worker.startup_reconciliation_completed_at is None

    release_reconciliation.set()
    await start_task
    try:
        assert worker.lifecycle_state == "ACCEPTING"
        assert worker.accepting_work
        assert worker.startup_reconciliation_completed_at is not None
        assert worker.startup_recovered_execution_count == 2
        assert worker.startup_cleanup_target_count == 0
    finally:
        await worker.stop()


async def test_startup_reconciliation_failure_keeps_worker_stopped(
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(engine, tmp_path)

    async def fail_reconciliation() -> ExpiredLeaseRecovery:
        raise RuntimeError("expected startup recovery failure")

    monkeypatch.setattr(
        worker._lease_recovery,
        "fence_expired_leases",
        fail_reconciliation,
    )

    with pytest.raises(
        RuntimeError, match="expected startup recovery failure"
    ):
        await worker.start()

    assert worker.lifecycle_state == "STOPPED"
    assert not worker.accepting_work
    assert worker.startup_reconciliation_completed_at is None


async def test_stop_cancels_remaining_job_after_drain_deadline(
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    worker = _worker(engine, tmp_path, drain_timeout=0.01)
    await worker.start()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_job() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    worker._dispatcher.dispatch(uuid4(), blocked_job())
    await started.wait()
    await worker.stop()

    assert cancelled.is_set()
    assert worker.lifecycle_state == "STOPPED"
    assert worker.active_job_count == 0


async def test_readiness_fails_as_soon_as_worker_enters_drain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6399/15",
        runtime_enabled=True,
        shared_storage_root=tmp_path,
    )
    container = ApplicationContainer(settings)
    async with container.engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        )
        await connection.execute(
            text("INSERT INTO alembic_version VALUES ('0002')")
        )
    monkeypatch.setattr(container.redis, "ping", AsyncMock(return_value=True))
    container.execution_worker._stopped = False
    container.execution_worker._dispatcher.set_accepting(True)

    try:
        initial_checks = await container.readiness()
        assert initial_checks == {
            "postgresql": True,
            "redis": True,
            "worker_accepting": True,
        }
        await container.execution_worker.begin_drain()
        checks = await container.readiness()
    finally:
        await container.execution_worker.stop()
        await container.redis.aclose()
        await container.engine.dispose()

    assert checks["worker_accepting"] is False
