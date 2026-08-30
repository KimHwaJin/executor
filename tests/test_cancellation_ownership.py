"""Exclusive and recoverable cancellation ownership tests."""

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select, update
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
    OutboxStatus,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    TriggerType,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionEventORM,
    ExecutionORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_leases import (
    ExecutionLeaseLostError,
    require_active_lease,
)
from executor_service.infrastructure.execution_worker import ExecutionWorker
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from tests.runtime_credentials import runtime_credential_fields


class UnusedRedis:
    """A placeholder for tests that invoke no Redis operation."""


def _command(name: str) -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=f"cancellation-owner-{name}-{uuid4().hex}",
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="cancellation-owner-user",
        project_id="cancellation-owner-project",
        session_id=f"cancellation-owner-{name}",
        task_id="cancellation-owner-task",
        steps=(StepSpec(sequence=0, code="print('work')"),),
    )


def _target() -> RuntimeTargetORM:
    return RuntimeTargetORM(
        name=f"cancellation-owner-target-{uuid4().hex}",
        connection_config={"endpoint": "http://runtime.invalid:8888"},
        **runtime_credential_fields(),
        pool=RuntimePool.INTERACTIVE,
        status=RuntimeTargetStatus.ACTIVE,
        max_concurrent_executions=10,
        supported_profiles=["basic"],
        enabled=True,
    )


def _worker(
    engine: AsyncEngine,
    tmp_path: Path,
    consumer_name: str,
) -> ExecutionWorker:
    session_factory = create_session_factory(engine)
    settings = Settings(
        runtime_enabled=False,
        shared_storage_root=tmp_path,
        execution_consumer_name=consumer_name,
    )
    return ExecutionWorker(
        session_factory=session_factory,
        redis=cast(Redis, UnusedRedis()),
        settings=settings,
        registry=RuntimeTargetRegistry(session_factory, settings),
        artifact_manager=ExecutionArtifactManager(session_factory),
    )


async def _request_cancel(
    service: ExecutionService, execution_id: UUID
) -> None:
    await service.cancel(
        CancelExecutionCommand(
            execution_id=execution_id,
            idempotency_key=f"cancel-{execution_id}",
            reason="cancellation ownership test",
        )
    )


async def test_cancellation_claim_fences_the_running_worker(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(_target())
    execution = await execution_service.submit(_command("fence"))
    running_worker = _worker(engine, tmp_path, "running-worker")
    cancellation_worker = _worker(engine, tmp_path, "cancellation-worker")

    claimed = await running_worker._claimer.claim(execution.id)
    assert claimed is not None
    stale_lease = claimed[2]
    await _request_cancel(execution_service, execution.id)

    assert (
        await cancellation_worker._claimer.claim_cancellation(execution.id)
        is None
    )
    await running_worker._runner._finalizer.release_for_cancellation(
        stale_lease
    )
    cancellation = await cancellation_worker._claimer.claim_cancellation(
        execution.id
    )
    assert cancellation is not None
    assert cancellation.lease.fencing_token > stale_lease.fencing_token

    async with session_factory() as session, session.begin():
        with pytest.raises(ExecutionLeaseLostError):
            await require_active_lease(
                session,
                stale_lease,
                allowed_statuses=(ExecutionStatus.CANCEL_REQUESTED,),
            )


async def test_expired_cancellation_lease_is_taken_over_and_stale_final_is_rejected(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    execution = await execution_service.submit(_command("takeover"))
    await _request_cancel(execution_service, execution.id)
    first = _worker(engine, tmp_path, "cancellation-worker-a")
    second = _worker(engine, tmp_path, "cancellation-worker-b")

    first_work = await first._claimer.claim_cancellation(execution.id)
    assert first_work is not None
    assert await second._claimer.claim_cancellation(execution.id) is None

    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                cancellation_lease_expires_at=utc_now() - timedelta(seconds=1)
            )
        )

    second_work = await second._claimer.claim_cancellation(execution.id)
    assert second_work is not None
    assert second_work.lease.fencing_token > first_work.lease.fencing_token
    with pytest.raises(ExecutionLeaseLostError):
        await first._finalize_cancellation(
            first_work.lease,
            RuntimeSessionCleanupStatus.NOT_REQUIRED,
        )
    await second._finalize_cancellation(
        second_work.lease,
        RuntimeSessionCleanupStatus.NOT_REQUIRED,
    )

    async with session_factory() as session:
        persisted = await session.get(ExecutionORM, execution.id)
        cancelled_events = await session.scalar(
            select(func.count(OutboxEventORM.id))
            .join(
                ExecutionEventORM,
                ExecutionEventORM.id == OutboxEventORM.execution_event_id,
            )
            .where(
                ExecutionEventORM.execution_id == execution.id,
                ExecutionEventORM.event_type == "execution.completed",
                ExecutionEventORM.payload["status"].as_string() == "CANCELLED",
                OutboxEventORM.status == OutboxStatus.PENDING,
            )
        )
    assert persisted is not None
    assert persisted.status == ExecutionStatus.CANCELLED
    assert persisted.cancellation_lease_owner is None
    assert persisted.cancellation_lease_expires_at is None
    assert cancelled_events == 1


async def test_duplicate_local_cancellation_dispatch_keeps_the_live_job(
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(engine, tmp_path, "local-cancellation-worker")
    worker._dispatcher.set_accepting(True)
    execution_id = uuid4()
    started = asyncio.Event()
    release = asyncio.Event()
    invocations = 0

    async def blocked_cancellation(_execution_id: UUID) -> None:
        nonlocal invocations
        invocations += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(worker, "_cancel_execution", blocked_cancellation)
    worker._dispatcher.dispatch(
        execution_id,
        worker._cancel_execution(execution_id),
        replace=True,
    )
    await started.wait()
    worker._dispatcher.dispatch(
        execution_id,
        worker._cancel_execution(execution_id),
        replace=True,
    )
    await asyncio.sleep(0)

    assert worker.active_job_count == 1
    assert invocations == 1
    release.set()
    await worker._dispatcher.wait_idle()


async def test_local_cancellation_waits_for_execution_evidence_handoff(
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    worker = _worker(engine, tmp_path, "handoff-worker")
    worker._dispatcher.set_accepting(True)
    execution_id = uuid4()
    execution_started = asyncio.Event()
    allow_evidence_commit = asyncio.Event()
    evidence_committed = asyncio.Event()
    cancellation_started = asyncio.Event()

    async def active_execution() -> None:
        execution_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await allow_evidence_commit.wait()
            evidence_committed.set()
            raise

    async def cancellation() -> None:
        assert evidence_committed.is_set()
        cancellation_started.set()

    worker._dispatcher.dispatch(execution_id, active_execution())
    await execution_started.wait()
    worker._dispatcher.dispatch(execution_id, cancellation(), replace=True)
    await asyncio.sleep(0)
    assert not cancellation_started.is_set()

    allow_evidence_commit.set()
    await worker._dispatcher.wait_idle()
    assert evidence_committed.is_set()
    assert cancellation_started.is_set()
