"""Durable-state admission tests for the Execution Worker."""

from collections.abc import Coroutine
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    ExecutionStatus,
    OperationMode,
    TriggerType,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import ExecutionORM
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_worker import ExecutionWorker
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.settings import Settings


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
    redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    worker = ExecutionWorker(
        session_factory=session_factory,
        redis=redis,
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
    try:
        reconciled = await worker._work_admission.reconcile()
    finally:
        await redis.aclose()

    assert reconciled == 3
    assert set(dispatched) == {
        (queued.id, False),
        (finalizing.id, False),
        (cancelling.id, True),
    }
