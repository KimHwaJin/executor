"""Durable-state admission tests for the Execution Worker."""

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    OperationMode,
    RuntimePool,
    RuntimeTargetStatus,
    TriggerType,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_worker import ExecutionWorker
from executor_service.infrastructure.execution_worker.work_admission import (
    WorkAdmissionProcessor,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.settings import Settings
from tests.runtime_credentials import runtime_credential_fields


async def test_reconcile_takes_only_free_slots_and_rotates(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    executions = [
        await execution_service.submit(_command(str(index)))
        for index in range(5)
    ]
    cancelling = await execution_service.submit(_command("cancelling"))
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == executions[-1].id)
            .values(status=ExecutionStatus.FINALIZING)
        )
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == cancelling.id)
            .values(status=ExecutionStatus.CANCEL_REQUESTED)
        )
    seen: list[UUID] = []

    def dispatch(execution_id, coroutine, **kwargs):
        coroutine.close()
        seen.append(execution_id)

    processor = WorkAdmissionProcessor(
        factory,
        Mock(dispatch=dispatch, active_job_count=1),
        AsyncMock(),
        AsyncMock(),
        max_active_jobs=3,
    )
    # Cancellation needs no slot; runs fill the two free ones, oldest first.
    assert await processor.reconcile() == 3
    assert seen == [cancelling.id, executions[0].id, executions[1].id]
    # Blocked heads do not starve later runs: the next pass moves on.
    seen.clear()
    assert await processor.reconcile() == 3
    assert seen == [cancelling.id, executions[2].id, executions[3].id]
    seen.clear()
    assert await processor.reconcile() == 2
    assert seen == [cancelling.id, executions[4].id]
    seen.clear()
    assert await processor.reconcile() == 3  # Wraps for another fair pass.
    assert seen[1:] == [executions[0].id, executions[1].id]


async def test_full_worker_dispatches_only_cancellations(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    await execution_service.submit(_command("queued"))
    seen: list[UUID] = []

    def dispatch(execution_id, coroutine, **kwargs):
        coroutine.close()
        seen.append(execution_id)

    processor = WorkAdmissionProcessor(
        create_session_factory(engine),
        Mock(dispatch=dispatch, active_job_count=3),
        AsyncMock(),
        AsyncMock(),
        max_active_jobs=3,
    )
    assert await processor.reconcile() == 0
    assert seen == []


def _command(name: str) -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=f"work-admission-{name}-{uuid4().hex}",
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="work-admission-user",
        project_id="work-admission-project",
        session_id="work-admission-session",
        task_id=f"task-{name}",
        steps=(StepSpec(sequence=0, code="print('ok')"),),
    )


async def test_reconcile_dispatches_each_durable_work_state(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queued = await execution_service.submit(_command("queued"))
    finalizing = await execution_service.submit(_command("finalizing"))
    cancelling = await execution_service.submit(_command("cancelling"))
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == finalizing.id)
            .values(status=ExecutionStatus.FINALIZING)
        )
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == cancelling.id)
            .values(status=ExecutionStatus.CANCEL_REQUESTED)
        )

    settings = Settings(runtime_enabled=False, shared_storage_root=tmp_path)
    worker = ExecutionWorker(
        session_factory=session_factory,
        settings=settings,
        registry=RuntimeTargetRegistry(session_factory, settings),
        artifact_manager=ExecutionArtifactManager(session_factory),
    )
    dispatched: list[tuple[UUID, bool]] = []

    def record_dispatch(
        execution_id: UUID,
        coroutine: Coroutine[Any, Any, None],
        *,
        replace: bool = False,
    ) -> None:
        coroutine.close()
        dispatched.append((execution_id, replace))

    monkeypatch.setattr(worker._dispatcher, "dispatch", record_dispatch)
    reconciled = await worker._work_admission.reconcile()

    assert reconciled == 3
    assert set(dispatched) == {
        (queued.id, False),
        (finalizing.id, False),
        (cancelling.id, True),
    }


async def test_duplicate_dispatch_keeps_one_active_job(
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _worker(engine, tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    invocations = 0

    async def blocking_execution(_execution_id: UUID) -> None:
        nonlocal invocations
        invocations += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(worker._runner, "run", blocking_execution)
    execution_id = uuid4()
    worker._dispatcher.dispatch(execution_id, worker._runner.run(execution_id))
    await started.wait()
    worker._dispatcher.dispatch(execution_id, worker._runner.run(execution_id))
    assert worker.active_job_count == 1
    assert invocations == 1

    release.set()
    await worker._dispatcher.wait_idle()
    await asyncio.sleep(0)
    assert worker.active_job_count == 0


async def test_two_workers_create_only_one_execution_attempt(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    execution = await execution_service.submit(_command("claim-once"))
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                name=f"claim-target-{uuid4().hex}",
                connection_config={"endpoint": "http://127.0.0.1:9"},
                **runtime_credential_fields(),
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
                max_concurrent_executions=2,
                supported_profiles=["basic"],
                enabled=True,
            )
        )
    first_worker = _worker(engine, tmp_path)
    second_worker = _worker(engine, tmp_path)

    assert await first_worker._claimer.claim(execution.id) is not None
    assert await second_worker._claimer.claim(execution.id) is None
    async with session_factory() as session:
        attempts = await session.scalar(
            select(func.count(ExecutionAttemptORM.id)).where(
                ExecutionAttemptORM.execution_id == execution.id,
                ExecutionAttemptORM.status == AttemptStatus.RUNNING,
            )
        )
    assert attempts == 1


def _worker(engine: AsyncEngine, tmp_path: Path) -> ExecutionWorker:
    settings = Settings(runtime_enabled=False, shared_storage_root=tmp_path)
    session_factory = create_session_factory(engine)
    worker = ExecutionWorker(
        session_factory=session_factory,
        settings=settings,
        registry=RuntimeTargetRegistry(session_factory, settings),
        artifact_manager=ExecutionArtifactManager(session_factory),
    )
    # Exercise active-Worker internals without starting background loops.
    worker._dispatcher.set_accepting(True)
    worker._stopped = False
    return worker
