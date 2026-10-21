"""Admission by per-user and per-Target totals of kernel memory limits."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    AttemptStatus,
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.repositories import (
    SQLAlchemyUnitOfWork,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_worker.target_selector import (
    RuntimeTargetSelector,
)
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
)
from executor_service.settings import Settings
from tests.runtime_credentials import runtime_credential_fields
from tests.test_execution_service import submit_command
from tests.test_multi_worker_postgres import (
    postgres_engine as postgres_engine,
)

GIB = 1024**3


class Harness:
    def __init__(self, engine: AsyncEngine, tmp_path: Path) -> None:
        self.session_factory = create_session_factory(engine)
        self.service = ExecutionService(
            lambda: SQLAlchemyUnitOfWork(self.session_factory),
            {RuntimeType.JUPYTER: ("basic",)},
            FilesystemExecutionResultStore(tmp_path),
        )

    async def target(self, name: str, capacity: int | None) -> UUID:
        target = RuntimeTargetORM(
            name=name,
            connection_config={"endpoint": "http://runtime.invalid:8888"},
            **runtime_credential_fields(),
            pool=RuntimePool.INTERACTIVE,
            status=RuntimeTargetStatus.ACTIVE,
            max_concurrent_executions=100,
            supported_profiles=["basic"],
            enabled=True,
            memory_capacity_bytes=capacity,
        )
        async with self.session_factory() as session, session.begin():
            session.add(target)
        return target.id

    async def queued(self, user_id: str, limit: int) -> UUID:
        execution = await self.service.submit(
            replace(
                submit_command(uuid4().hex),
                user_id=user_id,
                memory_limit_bytes=limit,
            )
        )
        return execution.id

    async def running(self, user_id: str, limit: int, target_id: UUID) -> None:
        execution_id = await self.queued(user_id, limit)
        now = utc_now()
        async with self.session_factory() as session, session.begin():
            session.add(
                ExecutionAttemptORM(
                    id=uuid4(),
                    execution_id=execution_id,
                    attempt_number=1,
                    runtime_type=RuntimeType.JUPYTER,
                    runtime_profile="basic",
                    runtime_target_id=target_id,
                    status=AttemptStatus.RUNNING,
                    lease_owner="other-worker",
                    lease_expires_at=now + timedelta(minutes=1),
                    heartbeat_at=now,
                    fencing_token=1,
                    started_at=now,
                )
            )

    async def select(
        self, settings: Settings, execution_id: UUID
    ) -> str | None:
        async with self.session_factory() as session, session.begin():
            execution = await session.scalar(
                select(ExecutionORM).where(ExecutionORM.id == execution_id)
            )
            assert execution is not None
            target = await RuntimeTargetSelector(settings).select(
                session, execution
            )
            return None if target is None else target.name


async def test_user_waits_while_their_held_limits_fill_the_quota(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = Harness(engine, tmp_path)
    target_id = await harness.target("jupyter", None)
    await harness.running("analyst", 3 * GIB, target_id)
    await harness.running("analyst", 3 * GIB, target_id)
    settings = Settings(
        execution_default_memory_limit_bytes=2 * GIB,
        execution_user_memory_quota_bytes=8 * GIB,
    )

    assert (
        await harness.select(
            settings, await harness.queued("analyst", 3 * GIB)
        )
        is None
    )
    assert (
        await harness.select(
            settings, await harness.queued("analyst", 2 * GIB)
        )
        == "jupyter"
    )
    assert (
        await harness.select(settings, await harness.queued("other", 3 * GIB))
        == "jupyter"
    )


async def test_target_admits_only_limits_that_fit_its_memory(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = Harness(engine, tmp_path)
    busy = await harness.target("a-busy", 10 * GIB)
    await harness.target("b-free", 3 * GIB)
    await harness.running("analyst", 4 * GIB, busy)
    await harness.running("other", 4 * GIB, busy)
    settings = Settings(
        execution_default_memory_limit_bytes=2 * GIB,
        runtime_memory_overcommit_ratio=1.0,
    )

    # 8 GiB held on a-busy leaves 2 GiB; b-free has 3 GiB.
    assert (
        await harness.select(settings, await harness.queued("new", 3 * GIB))
        == "b-free"
    )
    assert (
        await harness.select(settings, await harness.queued("new", 5 * GIB))
        is None
    )
    overcommit = Settings(
        execution_default_memory_limit_bytes=2 * GIB,
        runtime_memory_overcommit_ratio=1.5,
    )
    assert (
        await harness.select(overcommit, await harness.queued("new", 5 * GIB))
        == "a-busy"
    )


async def test_memory_admission_is_off_by_default(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    harness = Harness(engine, tmp_path)
    target_id = await harness.target("jupyter", 4 * GIB)
    await harness.running("analyst", 4 * GIB, target_id)

    assert (
        await harness.select(
            Settings(), await harness.queued("analyst", 4 * GIB)
        )
        == "jupyter"
    )


@pytest.mark.postgres
async def test_user_quota_on_postgres(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    await test_user_waits_while_their_held_limits_fill_the_quota(
        postgres_engine, tmp_path
    )
