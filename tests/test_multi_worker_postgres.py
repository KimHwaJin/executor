"""Real PostgreSQL concurrency checks for multiple Executor workers."""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from executor_service.application.commands import (
    CancelExecutionCommand,
    RetryExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.maintenance import (
    SetExecutorAdmissionCommand,
)
from executor_service.application.maintenance_runs import (
    CreateMaintenanceRunCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import (
    ActorType,
    AttemptStatus,
    ExecutionStatus,
    ExecutorAdmissionState,
    FailureType,
    MaintenanceRunAction,
    MaintenanceRunStatus,
    OperationMode,
    OperationStatus,
    OutboxStatus,
    RetryStrategy,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    RuntimeType,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import utc_now
from executor_service.events import build_execution_event
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionEventORM,
    ExecutionEventSequenceORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepORM,
    MaintenanceRunORM,
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
from executor_service.infrastructure.event_retention import (
    EventRetentionManager,
)
from executor_service.infrastructure.execution_leases import (
    ExecutionLeaseLostError,
)
from executor_service.infrastructure.execution_worker import ExecutionWorker
from executor_service.infrastructure.maintenance import (
    ExecutorMaintenanceService,
)
from executor_service.infrastructure.maintenance_runs import (
    MaintenanceRunService,
)
from executor_service.infrastructure.outbox import OutboxPublisher
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.tracing import TracingManager
from scripts.postgres_query_plan_smoke import (
    ORDERED_EVENT_PUBLICATION_QUERY,
)
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


def _server(
    *, capacity: int, active_session_count: int | None = None
) -> RuntimeTargetORM:
    return RuntimeTargetORM(
        name=f"postgres-race-target-{uuid4().hex}",
        connection_config={"endpoint": "http://127.0.0.1:9"},
        **runtime_credential_fields(),
        pool=RuntimePool.INTERACTIVE,
        status=RuntimeTargetStatus.ACTIVE,
        max_concurrent_executions=capacity,
        supported_profiles=["basic"],
        enabled=True,
        active_session_count=active_session_count,
        session_count_observed_at=(
            utc_now() if active_session_count is not None else None
        ),
    )


def _service(engine: AsyncEngine, root: Path) -> ExecutionService:
    session_factory = create_session_factory(engine)
    return ExecutionService(
        lambda: SQLAlchemyUnitOfWork(session_factory),
        {RuntimeType.JUPYTER: ("basic", "ml")},
        FilesystemExecutionResultStore(root),
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
            shared_storage_root=tmp_path / f"worker-{index}",
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
    service = _service(postgres_engine, tmp_path)
    session_factory = create_session_factory(postgres_engine)
    async with session_factory() as session, session.begin():
        session.add(_server(capacity=10))
    execution = await service.submit(_command("single-claim"))
    workers, redis_clients = _workers(postgres_engine, tmp_path, count=8)
    try:
        claims = await asyncio.gather(
            *(worker._claimer.claim(execution.id) for worker in workers)
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


async def test_persistent_drain_blocks_every_worker_until_activate(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine, tmp_path)
    session_factory = create_session_factory(postgres_engine)
    maintenance = ExecutorMaintenanceService(session_factory)
    async with session_factory() as session, session.begin():
        session.add(_server(capacity=10))
    executions = [
        await service.submit(_command(f"drained-{index}"))
        for index in range(6)
    ]
    await maintenance.set_state(
        SetExecutorAdmissionCommand(
            idempotency_key="postgres-global-drain",
            desired_state=ExecutorAdmissionState.DRAINING,
            actor_type=ActorType.USER,
            actor_id="postgres-operator",
        )
    )
    workers, redis_clients = _workers(postgres_engine, tmp_path, count=6)
    try:
        blocked = await asyncio.gather(
            *(
                workers[index]._claimer.claim(execution.id)
                for index, execution in enumerate(executions)
            )
        )
        drained_view = await maintenance.get()
        await maintenance.set_state(
            SetExecutorAdmissionCommand(
                idempotency_key="postgres-global-activate",
                desired_state=ExecutorAdmissionState.ACTIVE,
                actor_type=ActorType.USER,
                actor_id="postgres-operator",
            )
        )
        # Target rows use SKIP LOCKED, so a simultaneous one-target burst may
        # intentionally defer contenders to reconciliation. Claim sequentially
        # here to isolate global admission from target-lock contention.
        admitted = [
            await workers[index]._claimer.claim(execution.id)
            for index, execution in enumerate(executions)
        ]
    finally:
        await _close_redis(redis_clients)

    assert all(claim is None for claim in blocked)
    assert drained_view.queued_execution_count == len(executions)
    assert not drained_view.accepting_new_executions
    assert all(claim is not None for claim in admitted)


async def test_maintenance_run_has_one_owner_and_recovers_after_lease_expiry(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine, tmp_path)
    session_factory = create_session_factory(postgres_engine)
    execution = await service.submit(_command("maintenance-run-lease"))
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.RUNNING,
                runtime_session_id="maintenance-run-session",
            )
        )
    runs = MaintenanceRunService(session_factory, service, lease_seconds=30)
    created = await runs.create(
        CreateMaintenanceRunCommand(
            idempotency_key="postgres-maintenance-run",
            action=MaintenanceRunAction.STOP_ACTIVE_EXECUTIONS,
            actor_type=ActorType.USER,
            actor_id="postgres-operator",
        )
    )

    claims = await asyncio.gather(
        *(
            runs.reconcile_once(f"maintenance-worker-{index}")
            for index in range(6)
        )
    )

    assert sum(claims) == 1
    after_request = await service.get(execution.id)
    assert after_request.status == ExecutionStatus.CANCEL_REQUESTED
    async with session_factory() as session, session.begin():
        run = await session.get(MaintenanceRunORM, created.id)
        assert run is not None
        first_fence = run.fencing_token
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.CANCELLED,
                runtime_session_id=None,
                runtime_session_cleanup_status=(
                    RuntimeSessionCleanupStatus.SUCCEEDED
                ),
            )
        )
        run.lease_expires_at = utc_now() - timedelta(seconds=1)

    assert await runs.reconcile_once("maintenance-recovery-worker")
    completed = await runs.get(created.id)
    async with session_factory() as session:
        recovered = await session.get(MaintenanceRunORM, created.id)

    assert completed.status == MaintenanceRunStatus.SUCCEEDED
    assert completed.counts.stopped == 1
    assert recovered is not None
    assert recovered.fencing_token == first_fence + 1


async def test_concurrent_workers_claim_exactly_one_cancellation_owner(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine, tmp_path)
    execution = await service.submit(_command("single-cancellation-owner"))
    await service.cancel(
        CancelExecutionCommand(
            execution_id=execution.id,
            idempotency_key=f"postgres-cancel-{execution.id}",
            reason="exclusive cancellation owner verification",
        )
    )
    workers, redis_clients = _workers(postgres_engine, tmp_path, count=8)
    try:
        claims = await asyncio.gather(
            *(
                worker._claimer.claim_cancellation(execution.id)
                for worker in workers
            )
        )
        winners = [claim for claim in claims if claim is not None]
        assert len(winners) == 1
        winner = winners[0]
        await workers[
            next(index for index, claim in enumerate(claims) if claim)
        ]._finalize_cancellation(
            winner.lease,
            RuntimeSessionCleanupStatus.NOT_REQUIRED,
        )
    finally:
        await _close_redis(redis_clients)

    session_factory = create_session_factory(postgres_engine)
    async with session_factory() as session:
        persisted = await session.get(ExecutionORM, execution.id)
        cancelled_events = await session.scalar(
            select(func.count(ExecutionEventORM.id)).where(
                ExecutionEventORM.execution_id == execution.id,
                ExecutionEventORM.event_type == "execution.completed",
                ExecutionEventORM.payload["status"].as_string() == "CANCELLED",
            )
        )
    assert persisted is not None
    assert persisted.status == ExecutionStatus.CANCELLED
    assert persisted.cancellation_lease_owner is None
    assert cancelled_events == 1


async def test_stale_worker_cannot_write_or_heartbeat_after_takeover(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine, tmp_path)
    session_factory = create_session_factory(postgres_engine)
    async with session_factory() as session, session.begin():
        session.add(_server(capacity=2))
    execution = await service.submit(_command("lease-fence-takeover"))
    workers, redis_clients = _workers(postgres_engine, tmp_path, count=2)
    try:
        first_claim = await workers[0]._claimer.claim(execution.id)
        assert first_claim is not None
        stale_lease = first_claim[2]
        expired_at = utc_now() - timedelta(seconds=1)
        async with session_factory() as session, session.begin():
            await session.execute(
                update(ExecutionORM)
                .where(ExecutionORM.id == execution.id)
                .values(lease_expires_at=expired_at)
            )
            await session.execute(
                update(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.id == stale_lease.attempt_id)
                .values(lease_expires_at=expired_at)
            )

        await workers[1]._recover_expired_leases()
        await service.retry_result(
            RetryExecutionCommand(
                execution_id=execution.id,
                idempotency_key=f"fence-retry-{execution.id}",
            )
        )
        second_claim = await workers[1]._claimer.claim(execution.id)
        assert second_claim is not None
        current_lease = second_claim[2]
        assert current_lease.fencing_token > stale_lease.fencing_token

        with pytest.raises(ExecutionLeaseLostError):
            await workers[0]._step_executor.mark_started(stale_lease, 0)
        with pytest.raises(ExecutionLeaseLostError):
            await workers[0]._lease_heartbeat.renew_execution(stale_lease)

        async with session_factory() as session:
            persisted = await session.get(ExecutionORM, execution.id)
            step = await session.scalar(
                select(ExecutionStepORM).where(
                    ExecutionStepORM.execution_id == execution.id,
                    ExecutionStepORM.sequence == 0,
                )
            )
            stale_step_events = await session.scalar(
                select(func.count(OutboxEventORM.id)).where(
                    OutboxEventORM.aggregate_id == execution.id,
                    OutboxEventORM.event_type == "execution.step_started",
                )
            )
        assert persisted is not None
        assert persisted.fencing_token == current_lease.fencing_token
        assert step is not None
        assert step.status == StepStatus.PENDING
        assert stale_step_events == 0
    finally:
        await _close_redis(redis_clients)


async def test_concurrent_startup_reconciliation_fences_expired_lease_once(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine, tmp_path)
    session_factory = create_session_factory(postgres_engine)
    async with session_factory() as session, session.begin():
        session.add(_server(capacity=2))
    execution = await service.submit(_command("startup-fence-race"))
    workers, redis_clients = _workers(postgres_engine, tmp_path, count=8)
    try:
        claimed = await workers[0]._claimer.claim(execution.id)
        assert claimed is not None
        stale_lease = claimed[2]
        expired_at = utc_now() - timedelta(seconds=1)
        async with session_factory() as session, session.begin():
            await session.execute(
                update(ExecutionORM)
                .where(ExecutionORM.id == execution.id)
                .values(lease_expires_at=expired_at)
            )
            await session.execute(
                update(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.id == stale_lease.attempt_id)
                .values(lease_expires_at=expired_at)
            )

        recoveries = await asyncio.gather(
            *(worker._fence_expired_leases() for worker in workers)
        )
    finally:
        await _close_redis(redis_clients)

    assert sum(result.execution_count for result in recoveries) == 1
    async with session_factory() as session:
        persisted = await session.get(ExecutionORM, execution.id)
        completed_event_count = await session.scalar(
            select(func.count(ExecutionEventORM.id)).where(
                ExecutionEventORM.execution_id == execution.id,
                ExecutionEventORM.event_type == "execution.completed",
                ExecutionEventORM.payload["status"].as_string() == "FAILED",
            )
        )
    assert persisted is not None
    assert persisted.status == ExecutionStatus.FAILED
    assert persisted.failure_type == FailureType.LEASE_EXPIRED
    assert persisted.fencing_token == stale_lease.fencing_token + 1
    assert persisted.recovery_count == 1
    assert completed_event_count == 1


async def test_concurrent_workers_create_one_attempt_for_a_requeued_operation(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine, tmp_path)
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
            *(worker._claimer.claim(execution.id) for worker in workers)
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
        operation = await session.get(
            ExecutionOperationORM, retry.operation_id
        )
    assert len(attempts) == 1
    assert operation is not None
    assert operation.status == OperationStatus.RUNNING
    assert operation.execution_attempt_id == attempts[0].id


async def test_concurrent_workers_never_oversubscribe_jupyter_capacity(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine, tmp_path)
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
                    workers[index % len(workers)]._claimer.claim(execution_id)
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


async def test_concurrent_workers_respect_fresh_observed_session_capacity(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine, tmp_path)
    session_factory = create_session_factory(postgres_engine)
    target = _server(capacity=2, active_session_count=2)
    async with session_factory() as session, session.begin():
        session.add(target)
    executions = [
        await service.submit(_command(f"observed-full-{index}"))
        for index in range(8)
    ]
    workers, redis_clients = _workers(postgres_engine, tmp_path, count=6)
    try:
        claims = await asyncio.gather(
            *(
                workers[index % len(workers)]._claimer.claim(execution.id)
                for index, execution in enumerate(executions)
            )
        )
    finally:
        await _close_redis(redis_clients)

    assert all(claim is None for claim in claims)
    async with session_factory() as session:
        attempt_count = await session.scalar(
            select(func.count(ExecutionAttemptORM.id)).where(
                ExecutionAttemptORM.runtime_target_id == target.id
            )
        )
    assert attempt_count == 0


async def test_cancel_and_claim_race_has_one_consistent_terminal_result(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine, tmp_path)
    session_factory = create_session_factory(postgres_engine)
    async with session_factory() as session, session.begin():
        session.add(_server(capacity=20))
    workers, redis_clients = _workers(postgres_engine, tmp_path, count=6)
    executions = [
        await service.submit(_command(f"cancel-{index}"))
        for index in range(12)
    ]
    try:
        claim_tasks = [
            asyncio.create_task(
                workers[index % len(workers)]._claimer.claim(execution.id)
            )
            for index, execution in enumerate(executions)
        ]
        cancel_tasks = [
            asyncio.create_task(
                service.cancel(
                    CancelExecutionCommand(
                        execution_id=execution.id,
                        idempotency_key=f"postgres-cancel-{execution.id}",
                        reason="PostgreSQL claim/cancel race verification",
                    )
                )
            )
            for execution in executions
        ]
        await asyncio.gather(*claim_tasks, *cancel_tasks)
        claims = [task.result() for task in claim_tasks]
        await asyncio.gather(
            *(
                workers[
                    index % len(workers)
                ]._release_execution_for_cancellation(claim[2])
                for index, claim in enumerate(claims)
                if claim is not None
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
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine, tmp_path)
    execution = await service.submit(_command("outbox-once"))
    session_factory = create_session_factory(postgres_engine)
    events = [
        build_execution_event(
            execution_id=execution.id,
            event_sequence=sequence,
            event_type="execution.started",
            payload={
                "status": "RUNNING",
                "runtime": {
                    "provider": "JUPYTER",
                    "profile": "basic",
                    "target_id": str(uuid4()),
                    "session_id": "kernel-outbox-once",
                },
            },
        )
        for sequence in (1, 2)
    ]
    async with session_factory() as session, session.begin():
        session.add_all(
            [ExecutionEventORM.from_domain(event) for event in events]
        )
        session.add_all(
            [OutboxEventORM.from_execution_event(event) for event in events]
        )
        session.add(
            ExecutionEventSequenceORM(
                execution_id=execution.id,
                last_sequence=2,
            )
        )
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
        first_round = await asyncio.gather(
            *(publisher.publish_batch() for publisher in publishers)
        )
        second_round = await asyncio.gather(
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
    assert sum(first_round) + sum(second_round) == 3
    assert len(messages) == 1
    assert len(event_messages) == 2
    assert [int(fields["event_sequence"]) for _, fields in event_messages] == [
        1,
        2,
    ]
    assert published_rows == 3


async def test_ordered_outbox_backlog_load(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    execution_count = int(
        os.getenv("EXECUTOR_OUTBOX_LOAD_EXECUTION_COUNT", "5")
    )
    events_per_execution = int(
        os.getenv("EXECUTOR_OUTBOX_LOAD_EVENTS_PER_EXECUTION", "100")
    )
    minimum_events_per_second = float(
        os.getenv("EXECUTOR_OUTBOX_LOAD_MIN_EVENTS_PER_SECOND", "0")
    )
    assert execution_count >= 1
    assert events_per_execution >= 1

    service = _service(postgres_engine, tmp_path)
    executions = [
        await service.submit(_command(f"outbox-load-{index}"))
        for index in range(execution_count)
    ]
    target_id = uuid4()
    session_factory = create_session_factory(postgres_engine)
    events = [
        build_execution_event(
            execution_id=execution.id,
            event_sequence=sequence,
            event_type="execution.started",
            payload={
                "status": "RUNNING",
                "runtime": {
                    "provider": "JUPYTER",
                    "profile": "basic",
                    "target_id": str(target_id),
                    "session_id": f"load-{execution.id}",
                },
            },
        )
        for execution in executions
        for sequence in range(1, events_per_execution + 1)
    ]
    async with session_factory() as session, session.begin():
        session.add_all(
            [ExecutionEventORM.from_domain(event) for event in events]
        )
        session.add_all(
            [OutboxEventORM.from_execution_event(event) for event in events]
        )
        session.add_all(
            [
                ExecutionEventSequenceORM(
                    execution_id=execution.id,
                    last_sequence=events_per_execution,
                )
                for execution in executions
            ]
        )

    unique = uuid4().hex
    work_stream = f"test:executor:outbox-load:{unique}:work"
    event_stream = f"test:executor:outbox-load:{unique}:events"
    settings = Settings(
        runtime_enabled=False,
        redis_work_stream=work_stream,
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
            work_stream_name=work_stream,
            event_stream_name=event_stream,
            poll_interval_seconds=0.01,
            batch_size=100,
            tracing=TracingManager(settings),
        )
        for redis in redis_clients
    ]
    expected_count = execution_count * (events_per_execution + 1)
    published_count = 0
    rounds = 0
    started_at = perf_counter()
    try:
        while published_count < expected_count:
            counts = await asyncio.gather(
                *(publisher.publish_batch() for publisher in publishers)
            )
            published_this_round = sum(counts)
            if published_this_round == 0:
                raise AssertionError(
                    "Ordered Outbox backlog stopped making progress."
                )
            published_count += published_this_round
            rounds += 1
            if rounds > events_per_execution + execution_count + 10:
                raise AssertionError(
                    "Ordered Outbox backlog required unexpected extra rounds."
                )
        elapsed_seconds = perf_counter() - started_at
        event_messages = await redis_clients[0].xrange(event_stream)
        work_messages = await redis_clients[0].xrange(work_stream)
    finally:
        await redis_clients[0].delete(work_stream, event_stream)
        await _close_redis(redis_clients)

    sequences_by_execution = {
        str(execution.id): [] for execution in executions
    }
    for _, fields in event_messages:
        sequences_by_execution[fields["execution_id"]].append(
            int(fields["event_sequence"])
        )
    expected_sequences = list(range(1, events_per_execution + 1))
    assert all(
        sequences == expected_sequences
        for sequences in sequences_by_execution.values()
    )
    assert len(event_messages) == execution_count * events_per_execution
    assert len(work_messages) == execution_count
    events_per_second = len(event_messages) / elapsed_seconds
    print(
        {
            "execution_count": execution_count,
            "events_per_execution": events_per_execution,
            "published_event_count": len(event_messages),
            "publisher_rounds": rounds,
            "elapsed_seconds": round(elapsed_seconds, 6),
            "events_per_second": round(events_per_second, 2),
        }
    )
    if minimum_events_per_second > 0:
        assert events_per_second >= minimum_events_per_second


async def test_ordered_outbox_query_uses_pending_event_index(
    postgres_engine: AsyncEngine,
) -> None:
    async with postgres_engine.begin() as connection:
        await connection.execute(text("SET LOCAL enable_seqscan = off"))
        result = await connection.execute(
            text(f"EXPLAIN (COSTS OFF) {ORDERED_EVENT_PUBLICATION_QUERY}")
        )
    plan = "\n".join(str(row[0]) for row in result)
    assert "ix_outbox_pending_event_order" in plan, plan


async def test_event_retention_has_one_postgres_lease_owner(
    postgres_engine: AsyncEngine,
) -> None:
    unique = uuid4().hex
    settings = Settings(
        runtime_enabled=False,
        redis_work_stream=f"test:retention-lease:{unique}:work",
        redis_event_stream=f"test:retention-lease:{unique}:events",
        redis_work_dead_letter_stream=(
            f"test:retention-lease:{unique}:work-dlq"
        ),
        redis_event_dead_letter_stream=(
            f"test:retention-lease:{unique}:event-dlq"
        ),
    )
    session_factory = create_session_factory(postgres_engine)
    redis_clients = [
        Redis.from_url(_redis_test_url(), decode_responses=True)
        for _ in range(2)
    ]
    managers = [
        EventRetentionManager(session_factory, redis, settings)
        for redis in redis_clients
    ]
    try:
        claims = await asyncio.gather(
            *(manager._acquire_lease() for manager in managers)
        )
        assert sum(claims) == 1
        winner = managers[claims.index(True)]
        loser = managers[claims.index(False)]
        assert await loser.run_once() is None
        await winner._release_lease(error=None)
        assert await loser.run_once() is not None
    finally:
        await _close_redis(redis_clients)


async def test_earlier_0001_event_outbox_is_bridged_without_data_loss(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine, tmp_path)
    execution = await service.submit(_command("0001-event-bridge"))
    event = build_execution_event(
        execution_id=execution.id,
        event_sequence=1,
        event_type="execution.started",
        payload={
            "status": "RUNNING",
            "runtime": {
                "provider": "JUPYTER",
                "profile": "basic",
                "target_id": str(uuid4()),
                "session_id": "legacy-session",
            },
        },
    )
    session_factory = create_session_factory(postgres_engine)
    async with session_factory() as session, session.begin():
        outbox = OutboxEventORM.from_execution_event(event)
        session.add(ExecutionEventORM.from_domain(event))
        session.add(outbox)
        await session.flush()
        legacy_event_id = outbox.id

    async with postgres_engine.begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE outbox_events DROP CONSTRAINT "
                "fk_outbox_events_execution_event_id_execution_events"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE outbox_events DROP CONSTRAINT "
                "uq_outbox_execution_event_id"
            )
        )
        await connection.execute(
            text(
                "ALTER TABLE outbox_events DROP CONSTRAINT "
                "ck_outbox_events_valid_outbox_content"
            )
        )
        await connection.execute(
            text(
                "UPDATE outbox_events AS outbox SET payload = events.payload "
                "FROM execution_events AS events "
                "WHERE outbox.execution_event_id = events.id"
            )
        )
        await connection.execute(
            text("ALTER TABLE outbox_events ALTER COLUMN payload SET NOT NULL")
        )
        await connection.execute(
            text("ALTER TABLE outbox_events DROP COLUMN execution_event_id")
        )
        await connection.execute(text("DROP TABLE execution_events"))
        await connection.execute(text("DROP TABLE event_retention_lease"))
        await connection.execute(
            text(
                "ALTER TABLE outbox_events ADD CONSTRAINT "
                "ck_outbox_events_valid_outbox_event_sequence CHECK ("
                "(destination = 'EVENTS' AND event_sequence >= 1) OR "
                "(destination = 'WORK' AND event_sequence IS NULL))"
            )
        )
        await connection.execute(
            text("UPDATE alembic_version SET version_num = '0001'")
        )

    database_url = postgres_engine.url.render_as_string(hide_password=False)
    await asyncio.to_thread(_upgrade_and_check_baseline, database_url)

    async with session_factory() as session:
        migrated_event = await session.get(
            ExecutionEventORM,
            legacy_event_id,
        )
        migrated_outbox = await session.scalar(
            select(OutboxEventORM).where(
                OutboxEventORM.execution_event_id == legacy_event_id
            )
        )
        revision = await session.scalar(
            text("SELECT version_num FROM alembic_version")
        )

    assert revision == "0002"
    assert migrated_event is not None
    assert migrated_event.execution_id == execution.id
    assert migrated_event.event_sequence == 1
    assert migrated_event.payload == event.payload
    assert migrated_outbox is not None
    assert migrated_outbox.id == legacy_event_id
    assert migrated_outbox.payload is None


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
