"""Real PostgreSQL concurrency checks for multiple Executor workers."""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from executor_service.application.commands import (
    CancelExecutionCommand,
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
    JupyterPool,
    JupyterServerStatus,
    OutboxStatus,
    TriggerType,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.base import Base
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    JupyterServerORM,
    OutboxEventORM,
)
from executor_service.infrastructure.db.repositories import SQLAlchemyUnitOfWork
from executor_service.infrastructure.db.session import create_engine, create_session_factory
from executor_service.infrastructure.jupyter_registry import JupyterServerRegistry
from executor_service.infrastructure.outbox import OutboxPublisher
from executor_service.infrastructure.worker import ExecutionWorker
from executor_service.tracing import TracingManager

pytestmark = pytest.mark.postgres


@pytest_asyncio.fixture
async def postgres_engine() -> AsyncIterator[AsyncEngine]:
    if os.getenv("EXECUTOR_RUN_POSTGRES_TESTS") != "1":
        pytest.skip()

    admin_url = make_url(
        os.getenv(
            "EXECUTOR_POSTGRES_TEST_ADMIN_URL",
            "postgresql+psycopg://executor:executor@127.0.0.1:5432/postgres",
        )
    )
    database_name = f"executor_test_{uuid4().hex}"
    database_url = admin_url.set(database=database_name)
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    engine = create_engine(database_url.render_as_string(hide_password=False))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                ),
                {"database_name": database_name},
            )
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        await admin_engine.dispose()


def _command(name: str) -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=f"postgres-race-{name}-{uuid4().hex}",
        mode=ExecutionMode.STATIC,
        trigger_type=TriggerType.INTERACTIVE,
        jupyter_pool=JupyterPool.INTERACTIVE,
        kernel_name="python3",
        code_source_type=CodeSourceType.INLINE,
        code=f"print('{name}')",
        code_path=None,
        requested_by_user_id="postgres-race-user",
        project_id="postgres-race-project",
        session_id=f"postgres-race-session-{name}",
        execution_plan_id=f"postgres-race-plan-{name}",
        steps=(StepSpec(sequence=0, tool_name=name),),
    )


def _server(*, capacity: int) -> JupyterServerORM:
    return JupyterServerORM(
        name=f"postgres-race-server-{uuid4().hex}",
        endpoint="http://127.0.0.1:9",
        credential_ref="settings:JUPYTER_TOKEN",
        pool=JupyterPool.INTERACTIVE,
        status=JupyterServerStatus.ACTIVE,
        max_concurrent_executions=capacity,
        supported_kernels=["python3"],
        enabled=True,
    )


def _service(engine: AsyncEngine) -> ExecutionService:
    session_factory = create_session_factory(engine)
    return ExecutionService(lambda: SQLAlchemyUnitOfWork(session_factory))


def _workers(
    engine: AsyncEngine, tmp_path: Path, *, count: int
) -> tuple[list[ExecutionWorker], list[Redis]]:
    session_factory = create_session_factory(engine)
    workers: list[ExecutionWorker] = []
    redis_clients: list[Redis] = []
    for index in range(count):
        redis = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
        settings = Settings(
            jupyter_enabled=False,
            workspace_host_root=tmp_path / f"worker-{index}",
            execution_consumer_name=f"postgres-worker-{index}",
        )
        workers.append(
            ExecutionWorker(
                session_factory=session_factory,
                redis=redis,
                settings=settings,
                registry=JupyterServerRegistry(session_factory, settings),
                artifact_manager=ExecutionArtifactManager(session_factory, settings),
            )
        )
        redis_clients.append(redis)
    return workers, redis_clients


async def _close_redis(clients: list[Redis]) -> None:
    await asyncio.gather(*(client.aclose() for client in clients))


async def test_concurrent_workers_create_exactly_one_attempt_for_an_execution(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine)
    session_factory = create_session_factory(postgres_engine)
    async with session_factory() as session, session.begin():
        session.add(_server(capacity=10))
    execution = await service.submit(_command("single-claim"))
    workers, redis_clients = _workers(postgres_engine, tmp_path, count=8)
    try:
        claims = await asyncio.gather(
            *(worker._claim(execution.id) for worker in workers)
        )
    finally:
        await _close_redis(redis_clients)

    assert sum(claim is not None for claim in claims) == 1
    async with session_factory() as session:
        attempt_count = await session.scalar(
            select(func.count(ExecutionAttemptORM.id)).where(
                ExecutionAttemptORM.execution_id == execution.id
            )
        )
    assert attempt_count == 1


async def test_concurrent_workers_never_oversubscribe_jupyter_capacity(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine)
    session_factory = create_session_factory(postgres_engine)
    server = _server(capacity=2)
    async with session_factory() as session, session.begin():
        session.add(server)
    for index in range(12):
        await service.submit(_command(f"capacity-{index}"))
    workers, redis_clients = _workers(postgres_engine, tmp_path, count=6)
    try:
        for _ in range(4):
            async with session_factory() as session:
                queued_ids = list(
                    await session.scalars(
                        select(ExecutionORM.id).where(
                            ExecutionORM.status == ExecutionStatus.QUEUED
                        )
                    )
                )
            if not queued_ids:
                break
            await asyncio.gather(
                *(
                    workers[index % len(workers)]._claim(execution_id)
                    for index, execution_id in enumerate(queued_ids)
                )
            )
    finally:
        await _close_redis(redis_clients)

    async with session_factory() as session:
        running_count = await session.scalar(
            select(func.count(ExecutionAttemptORM.id)).where(
                ExecutionAttemptORM.jupyter_server_id == server.id,
                ExecutionAttemptORM.status == AttemptStatus.RUNNING,
            )
        )
        queued_count = await session.scalar(
            select(func.count(ExecutionORM.id)).where(
                ExecutionORM.status == ExecutionStatus.QUEUED
            )
        )
    assert running_count == 2
    assert queued_count == 10


async def test_cancel_and_claim_race_has_one_consistent_terminal_result(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine)
    session_factory = create_session_factory(postgres_engine)
    async with session_factory() as session, session.begin():
        session.add(_server(capacity=20))
    workers, redis_clients = _workers(postgres_engine, tmp_path, count=6)
    executions = [await service.submit(_command(f"cancel-{index}")) for index in range(12)]
    try:
        await asyncio.gather(
            *(
                operation
                for index, execution in enumerate(executions)
                for operation in (
                    workers[index % len(workers)]._claim(execution.id),
                    service.cancel(
                        CancelExecutionCommand(
                            execution_id=execution.id,
                            idempotency_key=f"postgres-cancel-{execution.id}",
                            reason="PostgreSQL claim/cancel race verification",
                        )
                    ),
                )
            )
        )
        await asyncio.gather(
            *(
                workers[index % len(workers)]._cancel_execution(execution.id)
                for index, execution in enumerate(executions)
            )
        )
    finally:
        await _close_redis(redis_clients)

    async with session_factory() as session:
        statuses = list(
            await session.scalars(
                select(ExecutionORM.status).where(
                    ExecutionORM.id.in_([execution.id for execution in executions])
                )
            )
        )
        running_attempts = await session.scalar(
            select(func.count(ExecutionAttemptORM.id)).where(
                ExecutionAttemptORM.status == AttemptStatus.RUNNING
            )
        )
    assert statuses == [ExecutionStatus.CANCELLED] * len(executions)
    assert running_attempts == 0


async def test_concurrent_outbox_publishers_emit_one_stream_message(
    postgres_engine: AsyncEngine,
) -> None:
    service = _service(postgres_engine)
    await service.submit(_command("outbox-once"))
    session_factory = create_session_factory(postgres_engine)
    unique = uuid4().hex
    stream = f"test:executor:postgres-outbox:{unique}"
    settings = Settings(jupyter_enabled=False, redis_stream=stream)
    redis_clients = [
        Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
        for _ in range(2)
    ]
    publishers = [
        OutboxPublisher(
            session_factory=session_factory,
            redis=redis,
            stream_name=stream,
            poll_interval_seconds=1,
            batch_size=100,
            tracing=TracingManager(settings),
        )
        for redis in redis_clients
    ]
    messages: list[tuple[str, dict[str, str]]] = []
    try:
        published = await asyncio.gather(
            *(publisher.publish_batch() for publisher in publishers)
        )
        messages = await redis_clients[0].xrange(stream)
    finally:
        await redis_clients[0].delete(stream)
        await _close_redis(redis_clients)

    async with session_factory() as session:
        published_rows = await session.scalar(
            select(func.count(OutboxEventORM.id)).where(
                OutboxEventORM.status == OutboxStatus.PUBLISHED
            )
        )
    assert sum(published) == 1
    assert len(messages) == 1
    assert published_rows == 1
