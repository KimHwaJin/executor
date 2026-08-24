"""Real PostgreSQL concurrency checks for multiple Executor workers."""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from executor_service.application.commands import (
    CancelExecutionCommand,
    RetryExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    OperationMode,
    OperationStatus,
    OutboxStatus,
    RetryStrategy,
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
    TriggerType,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.repositories import (
    SQLAlchemyUnitOfWork,
)
from executor_service.infrastructure.db.session import (
    create_engine,
    create_session_factory,
)
from executor_service.infrastructure.outbox import OutboxPublisher
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.infrastructure.worker import ExecutionWorker
from executor_service.tracing import TracingManager
from tests.runtime_credentials import runtime_credential_fields

pytestmark = pytest.mark.postgres


def _redis_test_url() -> str:
    return os.getenv("EXECUTOR_REDIS_TEST_URL", "redis://127.0.0.1:6379/15")


def _upgrade_and_check_baseline(database_url: str) -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    command.check(config)


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
        await asyncio.to_thread(
            _upgrade_and_check_baseline,
            database_url.render_as_string(hide_password=False),
        )
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
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database_name}"')
            )
        await admin_engine.dispose()


def _command(name: str) -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=f"postgres-race-{name}-{uuid4().hex}",
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="postgres-race-user",
        project_id="postgres-race-project",
        session_id=f"postgres-race-session-{name}",
        task_id="test-task",
        steps=(
            StepSpec(
                sequence=0,
                code=f"print('{name}')",
                tool_name=name,
            ),
        ),
    )


def _server(*, capacity: int) -> RuntimeTargetORM:
    return RuntimeTargetORM(
        name=f"postgres-race-target-{uuid4().hex}",
        connection_config={"endpoint": "http://127.0.0.1:9"},
        **runtime_credential_fields(),
        pool=RuntimePool.INTERACTIVE,
        status=RuntimeTargetStatus.ACTIVE,
        max_concurrent_executions=capacity,
        supported_profiles=["basic"],
        enabled=True,
    )


def _service(engine: AsyncEngine) -> ExecutionService:
    session_factory = create_session_factory(engine)
    return ExecutionService(
        lambda: SQLAlchemyUnitOfWork(session_factory),
        {RuntimeType.JUPYTER: ("basic", "ml")},
    )


def _workers(
    engine: AsyncEngine, tmp_path: Path, *, count: int
) -> tuple[list[ExecutionWorker], list[Redis]]:
    session_factory = create_session_factory(engine)
    workers: list[ExecutionWorker] = []
    redis_clients: list[Redis] = []
    for index in range(count):
        redis = Redis.from_url(_redis_test_url(), decode_responses=True)
        settings = Settings(
            runtime_enabled=False,
            input_host_root=tmp_path / f"worker-{index}",
            execution_consumer_name=f"postgres-worker-{index}",
        )
        workers.append(
            ExecutionWorker(
                session_factory=session_factory,
                redis=redis,
                settings=settings,
                registry=RuntimeTargetRegistry(session_factory, settings),
                artifact_manager=ExecutionArtifactManager(session_factory),
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


async def test_concurrent_workers_create_one_attempt_for_a_requeued_operation(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine)
    session_factory = create_session_factory(postgres_engine)
    async with session_factory() as session, session.begin():
        session.add(_server(capacity=10))
    execution = await service.submit(_command("retry-single-claim"))
    async with session_factory() as session, session.begin():
        persisted = await session.get(ExecutionORM, execution.id)
        assert persisted is not None
        operation = await session.get(
            ExecutionOperationORM, persisted.active_operation_id
        )
        assert operation is not None
        persisted.status = ExecutionStatus.FAILED
        persisted.retry_strategy = RetryStrategy.FROM_START
        persisted.retry_from_sequence = 0
        operation.status = OperationStatus.FAILED

    retry = await service.retry_result(
        RetryExecutionCommand(
            execution_id=execution.id,
            idempotency_key=f"postgres-retry-{execution.id}",
        )
    )
    assert retry.operation_id == execution.active_operation_id

    workers, redis_clients = _workers(postgres_engine, tmp_path, count=8)
    try:
        claims = await asyncio.gather(
            *(worker._claim(execution.id) for worker in workers)
        )
    finally:
        await _close_redis(redis_clients)

    assert sum(claim is not None for claim in claims) == 1
    async with session_factory() as session:
        attempts = list(
            await session.scalars(
                select(ExecutionAttemptORM).where(
                    ExecutionAttemptORM.execution_id == execution.id
                )
            )
        )
        operation = await session.get(ExecutionOperationORM, retry.operation_id)
    assert len(attempts) == 1
    assert operation is not None
    assert operation.status == OperationStatus.RUNNING
    assert operation.execution_attempt_id == attempts[0].id


async def test_concurrent_workers_never_oversubscribe_jupyter_capacity(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine)
    session_factory = create_session_factory(postgres_engine)
    target = _server(capacity=2)
    async with session_factory() as session, session.begin():
        session.add(target)
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
                ExecutionAttemptORM.runtime_target_id == target.id,
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
    executions = [
        await service.submit(_command(f"cancel-{index}")) for index in range(12)
    ]
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
                    ExecutionORM.id.in_(
                        [execution.id for execution in executions]
                    )
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
    event_stream = f"{stream}:events"
    settings = Settings(
        runtime_enabled=False,
        redis_work_stream=stream,
        redis_event_stream=event_stream,
    )
    redis_clients = [
        Redis.from_url(_redis_test_url(), decode_responses=True)
        for _ in range(2)
    ]
    publishers = [
        OutboxPublisher(
            session_factory=session_factory,
            redis=redis,
            work_stream_name=stream,
            event_stream_name=event_stream,
            poll_interval_seconds=1,
            batch_size=100,
            tracing=TracingManager(settings),
        )
        for redis in redis_clients
    ]
    messages: list[tuple[str, dict[str, str]]] = []
    event_messages: list[tuple[str, dict[str, str]]] = []
    try:
        published = await asyncio.gather(
            *(publisher.publish_batch() for publisher in publishers)
        )
        messages = await redis_clients[0].xrange(stream)
        event_messages = await redis_clients[0].xrange(event_stream)
    finally:
        await redis_clients[0].delete(stream, event_stream)
        await _close_redis(redis_clients)

    async with session_factory() as session:
        published_rows = await session.scalar(
            select(func.count(OutboxEventORM.id)).where(
                OutboxEventORM.status == OutboxStatus.PUBLISHED
            )
        )
    assert sum(published) == 2
    assert len(messages) == 1
    assert len(event_messages) == 1
    assert published_rows == 2


async def test_bounded_pool_times_out_instead_of_opening_unlimited_connections(
    postgres_engine: AsyncEngine,
) -> None:
    bounded_engine = create_engine(
        postgres_engine.url.render_as_string(hide_password=False),
        pool_size=1,
        max_overflow=0,
        pool_timeout_seconds=0.05,
    )
    try:
        async with bounded_engine.connect():
            with pytest.raises(SQLAlchemyTimeoutError):
                async with bounded_engine.connect():
                    pass
    finally:
        await bounded_engine.dispose()
