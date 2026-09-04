"""Real PostgreSQL concurrency checks for multiple Executor workers."""

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import func, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from executor_service.application.commands import (
    CancelExecutionCommand,
    CreateOperationCommand,
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
from executor_service.container import EXPECTED_SCHEMA_REVISION
from executor_service.domain.diagnostics import DiagnosticCategory
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
from executor_service.events import (
    ExecutionStreamEnvelope,
    build_execution_event,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.background_diagnostics import (
    BackgroundDiagnosticRecorder,
    RuntimeObservation,
)
from executor_service.infrastructure.db.base import Base
from executor_service.infrastructure.db.migrations import (
    MIGRATION_LOCK_ID,
    DatabaseMigrationError,
    upgrade_database,
)
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionDiagnosticORM,
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
from executor_service.infrastructure.diagnostic_store import (
    DiagnosticRecorder,
    SQLAlchemyDiagnosticQueryService,
)
from executor_service.infrastructure.event_retention import (
    EventRetentionManager,
)
from executor_service.infrastructure.execution_leases import (
    ExecutionLeaseLostError,
)
from executor_service.infrastructure.execution_worker import ExecutionWorker
from executor_service.infrastructure.execution_worker.run_finalizer import (
    ExecutionRunFinalizer,
)
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
from scripts.postgres_query_plan_smoke import (
    ORDERED_EVENT_PUBLICATION_QUERY,
)
from tests.result_evidence_assertions import assert_result_evidence_surfaces
from tests.runtime_credentials import runtime_credential_fields
from tests.test_runtime_failure_evidence import EvidenceDriver

pytestmark = pytest.mark.postgres
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _redis_test_url() -> str:
    return os.getenv("EXECUTOR_REDIS_TEST_URL", "redis://127.0.0.1:6379/15")


def _upgrade_and_check_baseline(database_url: str) -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
    command.check(config)


def _downgrade_baseline(database_url: str) -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.downgrade(config, "base")


async def test_startup_migration_creates_empty_db_and_repeats_safely(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    url = postgres_engine.url.render_as_string(hide_password=False)
    await asyncio.to_thread(_downgrade_baseline, url)
    settings = Settings(_env_file=None, db_auto_migrate=True)
    await upgrade_database(postgres_engine, settings)
    service = _service(postgres_engine, tmp_path)
    execution = await service.submit(_command("auto-migrated"))
    await upgrade_database(postgres_engine, settings)
    assert (await service.get(execution.id)).task_id == execution.task_id
    await asyncio.to_thread(_upgrade_and_check_baseline, url)


@pytest.mark.parametrize("manual_peer", [False, True])
async def test_startup_migrations_serialize_across_processes(
    postgres_engine: AsyncEngine,
    manual_peer: bool,
) -> None:
    url = postgres_engine.url.render_as_string(hide_password=False)
    await asyncio.to_thread(_downgrade_baseline, url)
    script = """
from executor_service.config import Settings
from executor_service.event_loop import run_async
from executor_service.infrastructure.db.session import create_engine
from executor_service.infrastructure.db.migrations import upgrade_database

async def main():
    settings = Settings(_env_file=None, db_migration_lock_timeout_seconds=20)
    engine = create_engine(settings.database_dsn)
    try:
        await upgrade_database(engine, settings)
    finally:
        await engine.dispose()

run_async(main())
"""
    manual_script = (
        "from alembic import command; from alembic.config import Config; "
        "command.upgrade(Config('alembic.ini'), 'head')"
    )
    environment = {
        **os.environ,
        "DATABASE_URL": url,
        "DB_MIGRATION_LOCK_TIMEOUT_SECONDS": "20",
    }
    jobs = []
    try:
        async with postgres_engine.begin() as holder:
            await holder.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": MIGRATION_LOCK_ID},
            )
            jobs = [
                asyncio.create_task(
                    asyncio.to_thread(
                        subprocess.run,
                        [
                            sys.executable,
                            "-c",
                            (
                                manual_script
                                if manual_peer and index == 1
                                else script
                            ),
                        ],
                        cwd=REPOSITORY_ROOT,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                )
                for index in range(2)
            ]
            for _ in range(100):
                async with postgres_engine.connect() as observer:
                    waiting = await observer.scalar(
                        text(
                            "SELECT count(*) FROM pg_locks WHERE locktype="
                            "'advisory' AND NOT granted AND database="
                            "(SELECT oid FROM pg_database "
                            "WHERE datname=current_database())"
                        )
                    )
                if waiting == 2:
                    break
                await asyncio.sleep(0.05)
            assert waiting == 2, (
                "Both migration processes must wait on DB lock"
            )
        results = await asyncio.gather(*jobs)
        assert all(r.returncode == 0 for r in results), [
            r.stderr for r in results
        ]
        await asyncio.to_thread(_upgrade_and_check_baseline, url)
    finally:
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)


async def test_startup_lock_timeout_and_cancellation_release_resources(
    postgres_engine: AsyncEngine,
) -> None:
    settings = Settings(_env_file=None, db_migration_lock_timeout_seconds=1)
    async with postgres_engine.begin() as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": MIGRATION_LOCK_ID},
        )
        with pytest.raises(DatabaseMigrationError, match="55P03"):
            await upgrade_database(postgres_engine, settings)
        task = asyncio.create_task(upgrade_database(postgres_engine, settings))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await upgrade_database(postgres_engine, settings)


async def test_startup_failure_rolls_back_and_preserves_safe_logging(
    postgres_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    # The fixture's manual Alembic fileConfig disables preexisting loggers.
    # Restore this one to model app startup after configure_logging().
    monkeypatch.setattr(
        logging.getLogger("executor_service.infrastructure.db.migrations"),
        "disabled",
        False,
    )
    handlers = list(logging.getLogger().handlers)
    original = command.upgrade

    def fail(config: Config, revision: str) -> None:
        config.attributes["connection"].execute(
            text("CREATE TABLE startup_migration_rollback_test (id integer)")
        )
        raise RuntimeError("password=must-not-leak")

    monkeypatch.setattr(command, "upgrade", fail)
    with pytest.raises(DatabaseMigrationError) as raised:
        await upgrade_database(postgres_engine, Settings(_env_file=None))
    assert "must-not-leak" not in str(raised.value)
    assert "must-not-leak" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert logging.getLogger().handlers == handlers
    async with postgres_engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT to_regclass('startup_migration_rollback_test')")
            )
            is None
        )
    monkeypatch.setattr(command, "upgrade", original)
    await upgrade_database(postgres_engine, Settings(_env_file=None))
    assert logging.getLogger().handlers == handlers


def _downgrade_to_pre_diagnostics(database_url: str) -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.downgrade(config, "0001")


def _downgrade_to_diagnostics(database_url: str) -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.downgrade(config, "0002")


def _downgrade_to_pre_trace_removal(database_url: str) -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.attributes["database_url"] = database_url
    command.downgrade(config, "0003")


async def test_trace_removal_preserves_business_rows_and_restores_only_schema(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    url = postgres_engine.url.render_as_string(hide_password=False)
    await asyncio.to_thread(_downgrade_to_pre_trace_removal, url)
    service = _service(postgres_engine, tmp_path)
    execution = await service.submit(_command("remove-traces"))
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
                "session_id": "migration-test-session",
            },
        },
    )
    factory = create_session_factory(postgres_engine)
    async with factory() as session, session.begin():
        session.add(ExecutionEventORM.from_domain(event))
        session.add(OutboxEventORM.from_execution_event(event))

    trace_tables = ("executions", "execution_events", "outbox_events")
    # Include dependent history to detect unintended cascade/data changes.
    tables = (*trace_tables, "execution_operations", "execution_steps")
    before = {}
    async with postgres_engine.begin() as connection:
        for table in trace_tables:
            await connection.execute(
                text(
                    f"UPDATE {table} SET traceparent=:parent, "
                    "tracestate=:state"
                ),
                {"parent": "old-trace-parent", "state": "old-trace-state"},
            )
        for table in tables:
            rows = (
                (
                    await connection.execute(
                        text(f"SELECT * FROM {table} ORDER BY id")
                    )
                )
                .mappings()
                .all()
            )
            assert rows
            before[table] = [
                {
                    k: v
                    for k, v in row.items()
                    if k not in {"traceparent", "tracestate"}
                }
                for row in rows
            ]

    await asyncio.to_thread(_upgrade_and_check_baseline, url)
    async with postgres_engine.connect() as connection:
        for table in tables:
            rows = (
                (
                    await connection.execute(
                        text(f"SELECT * FROM {table} ORDER BY id")
                    )
                )
                .mappings()
                .all()
            )
            assert [dict(row) for row in rows] == before[table]
        columns = await connection.run_sync(
            lambda sync: {
                table: {c["name"] for c in inspect(sync).get_columns(table)}
                for table in trace_tables
            }
        )
        assert all(
            not {"traceparent", "tracestate"} & names
            for names in columns.values()
        )

    await asyncio.to_thread(_downgrade_to_pre_trace_removal, url)
    async with postgres_engine.connect() as connection:
        for table in trace_tables:
            rows = (
                (
                    await connection.execute(
                        text(f"SELECT * FROM {table} ORDER BY id")
                    )
                )
                .mappings()
                .all()
            )
            assert all(
                row["traceparent"] is None and row["tracestate"] is None
                for row in rows
            )
            assert [
                {
                    k: v
                    for k, v in row.items()
                    if k not in {"traceparent", "tracestate"}
                }
                for row in rows
            ] == before[table]
    await asyncio.to_thread(_upgrade_and_check_baseline, url)
    assert (await service.get(execution.id)).task_id == execution.task_id


async def test_completion_upgrade_preserves_rows_and_blocks_lossy_downgrade(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    from sqlalchemy.exc import IntegrityError

    url = postgres_engine.url.render_as_string(hide_password=False)
    await asyncio.to_thread(_downgrade_to_diagnostics, url)
    service = _service(postgres_engine, tmp_path)
    execution = await service.submit(_command("before-completion-upgrade"))
    factory = create_session_factory(postgres_engine)
    async with factory() as session, session.begin():
        session.add(_server(capacity=1))
    workers, clients = _workers(postgres_engine, tmp_path, count=1)
    try:
        claim = await workers[0]._claimer.claim(execution.id)
        assert claim is not None
        lease = claim[2]
        await asyncio.to_thread(_upgrade_and_check_baseline, url)
        await ExecutionRunFinalizer(factory, Settings()).finalize(
            lease,
            ExecutionStatus.FAILED,
            "Required completion failed.",
            failure_type=FailureType.COMPLETION_FAILED,
            retry_strategy=RetryStrategy.NOT_RETRYABLE,
        )
        with pytest.raises(IntegrityError):
            await asyncio.to_thread(_downgrade_to_diagnostics, url)
        async with factory() as session:
            row = await session.get(ExecutionORM, execution.id)
            attempt = await session.get(ExecutionAttemptORM, lease.attempt_id)
            revision = await session.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            assert revision == EXPECTED_SCHEMA_REVISION
            assert (
                row is not None
                and row.failure_type == FailureType.COMPLETION_FAILED
            )
            assert (
                attempt is not None
                and attempt.failure_type == FailureType.COMPLETION_FAILED
            )
            assert row.task_id == execution.task_id
    finally:
        await _close_redis(clients)


async def test_diagnostics_upgrade_preserves_existing_execution_and_cascades(
    postgres_engine: AsyncEngine, tmp_path: Path
) -> None:
    url = postgres_engine.url.render_as_string(hide_password=False)
    await asyncio.to_thread(_downgrade_to_pre_diagnostics, url)
    service = _service(postgres_engine, tmp_path)
    execution = await service.submit(_command("preserve-before-upgrade"))
    await asyncio.to_thread(_upgrade_and_check_baseline, url)
    assert (await service.get(execution.id)).task_id == execution.task_id
    factory = create_session_factory(postgres_engine)
    async with factory() as session, session.begin():
        session.add(_server(capacity=2))
    workers, clients = _workers(postgres_engine, tmp_path, count=1)
    try:
        claim = await workers[0]._claimer.claim(execution.id)
        assert claim is not None
        lease = claim[2]
        recorder = DiagnosticRecorder(factory)
        assert all(
            await asyncio.gather(
                *(
                    recorder.record(
                        lease,
                        PermissionError(13, "private"),
                        phase="RESULT_APPEND",
                        category=DiagnosticCategory.OUTPUT,
                        sequence=0,
                    )
                    for _ in range(4)
                )
            )
        )
        query = SQLAlchemyDiagnosticQueryService(factory)
        page = await query.list(execution.id, limit=2)
        assert page.next_cursor is not None
        tail = await query.list(execution.id, cursor=page.next_cursor)
        assert len(page.items) == len(tail.items) == 2
        assert len({item.id for item in page.items + tail.items}) == 4
        async with factory() as session, session.begin():
            await session.execute(
                update(ExecutionORM)
                .where(ExecutionORM.id == execution.id)
                .values(fencing_token=lease.fencing_token + 1)
            )
        assert not await recorder.record(
            lease,
            ValueError("stale"),
            phase="EXECUTION_RUN",
            category=DiagnosticCategory.EXECUTION,
        )
        from sqlalchemy import delete

        async with factory() as session, session.begin():
            await session.execute(
                delete(ExecutionORM).where(ExecutionORM.id == execution.id)
            )
        async with factory() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(ExecutionDiagnosticORM)
                )
                == 0
            )
    finally:
        await _close_redis(clients)


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


async def test_followup_multi_operation_flushes_parent_before_steps(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine, tmp_path)
    execution = await service.submit(
        SubmitExecutionCommand(
            idempotency_key=f"postgres-multi-submit-{uuid4().hex}",
            operation_mode=OperationMode.MULTI,
            operation_wait_timeout_seconds=600,
            trigger_type=TriggerType.INTERACTIVE,
            runtime_profile="basic",
            user_id="postgres-multi-user",
            project_id="postgres-multi-project",
            session_id="postgres-multi-session",
            task_id="postgres-multi-task",
            steps=(StepSpec(sequence=0, code="value = 40"),),
        )
    )
    session_factory = create_session_factory(postgres_engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(status=ExecutionStatus.WAITING_FOR_OPERATION, version=1)
        )

    continued = await service.create_operation(
        CreateOperationCommand(
            execution_id=execution.id,
            idempotency_key=f"postgres-multi-continue-{uuid4().hex}",
            expected_version=1,
            steps=(StepSpec(sequence=1, code="print(value + 2)"),),
        )
    )

    async with session_factory() as session:
        operation_count = await session.scalar(
            select(func.count(ExecutionOperationORM.id)).where(
                ExecutionOperationORM.execution_id == execution.id
            )
        )
        steps = tuple(
            await session.scalars(
                select(ExecutionStepORM)
                .where(ExecutionStepORM.execution_id == execution.id)
                .order_by(ExecutionStepORM.sequence)
            )
        )

    assert continued.status == ExecutionStatus.QUEUED
    assert continued.version == 2
    assert operation_count == 2
    assert [step.sequence for step in steps] == [0, 1]
    assert steps[1].operation_id == continued.active_operation_id


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
        ]._cancellation.finalize(
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
        store = FilesystemExecutionResultStore(tmp_path)
        identity = workers[0]._step_executor.result_identity(
            first_claim[0].steps[0], stale_lease
        )
        source = workers[0]._step_executor.source_reference(
            first_claim[0].steps[0]
        )
        await store.begin_step_result(identity, source)
        interrupted_result = await store.abort_step_result(
            identity, reason="stale cancellation evidence"
        )
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

        await workers[1]._lease_recovery.recover()
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
        with pytest.raises(ExecutionLeaseLostError):
            await workers[0]._step_executor._record_interrupted_result(
                stale_lease, 0, interrupted_result
            )

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
        assert step.result_manifest_path is None
        assert stale_step_events == 0
    finally:
        await _close_redis(redis_clients)


@pytest.mark.parametrize("mode", [OperationMode.SINGLE, OperationMode.MULTI])
async def test_partial_result_outbox_redis_and_history_agree(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: OperationMode,
) -> None:
    service = _service(postgres_engine, tmp_path)
    factory = create_session_factory(postgres_engine)
    async with factory() as session, session.begin():
        session.add(_server(capacity=1))
    command = replace(
        _command("partial-result-redis"),
        operation_mode=mode,
        operation_wait_timeout_seconds=600
        if mode == OperationMode.MULTI
        else None,
    )
    execution = await service.submit(command)
    driver = EvidenceDriver("disconnect", False)
    monkeypatch.setattr(
        "executor_service.infrastructure.runtime_drivers.JupyterRuntimeDriver",
        lambda *_args, **_kwargs: driver,
    )
    redis = Redis.from_url(_redis_test_url(), decode_responses=True)
    unique = f"test:partial-results:{uuid4().hex}"
    work_stream, event_stream = f"{unique}:work", f"{unique}:events"
    settings = Settings(runtime_enabled=False, shared_storage_root=tmp_path)
    worker = ExecutionWorker(
        session_factory=factory,
        redis=redis,
        settings=settings,
        registry=RuntimeTargetRegistry(factory, settings),
        artifact_manager=ExecutionArtifactManager(factory),
    )
    try:
        await worker._runner.run(execution.id)
        await assert_result_evidence_surfaces(factory, execution.id, tmp_path)
        publisher = OutboxPublisher(
            session_factory=factory,
            redis=redis,
            work_stream_name=work_stream,
            event_stream_name=event_stream,
            poll_interval_seconds=0.01,
            batch_size=100,
        )
        assert await publisher.publish_batch() > 0
        assert await publisher.publish_batch() == 0
        messages = await redis.xrange(event_stream)
        envelopes = [
            ExecutionStreamEnvelope.from_redis_fields(fields)
            for _, fields in messages
        ]
        assert [event.event_sequence for event in envelopes] == list(
            range(1, len(envelopes) + 1)
        )
        assert len(envelopes) == 6
        async with factory() as session:
            for envelope in envelopes:
                history = await session.get(
                    ExecutionEventORM, envelope.event_id
                )
                assert history is not None
                assert envelope.payload == history.payload
        step_event = next(
            item
            for item in envelopes
            if item.event_type == "execution.step_completed"
        )
        assert step_event.payload["result_ref"]["complete"] is False
        assert step_event.payload["output_summary"]["count"] == 1
        assert step_event.payload["status"] == "FAILED"
        assert "partial evidence" not in step_event.model_dump_json()
    finally:
        await redis.delete(work_stream, event_stream)
        await redis.aclose()


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
                .values(
                    lease_expires_at=expired_at,
                    notebook_path="executions/test/notebooks/execution.ipynb",
                    notebook_projection_status="SUCCEEDED",
                    notebook_projection_attempt_count=1,
                    notebook_projected_at=utc_now(),
                )
            )
            await session.execute(
                update(ExecutionAttemptORM)
                .where(ExecutionAttemptORM.id == stale_lease.attempt_id)
                .values(lease_expires_at=expired_at)
            )

        recoveries = await asyncio.gather(
            *(
                worker._lease_recovery.fence_expired_leases()
                for worker in workers
            )
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
    assert persisted.notebook_projection_status == "FAILED"
    assert persisted.notebook_projected_at is None
    assert persisted.notebook_projection_attempt_count == 1
    async with session_factory() as session:
        diagnostics = list(
            await session.scalars(
                select(ExecutionDiagnosticORM).where(
                    ExecutionDiagnosticORM.execution_id == execution.id
                )
            )
        )
    assert len(diagnostics) == 1
    assert diagnostics[0].detail["phase"] == "NOTEBOOK_LEASE_EXPIRED"
    assert diagnostics[0].fencing_token == stale_lease.fencing_token + 1


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
                ]._runner._finalizer.release_for_cancellation(claim[2])
                for index, claim in enumerate(claims)
                if claim is not None
            )
        )
        await asyncio.gather(
            *(
                workers[index % len(workers)]._cancellation.cancel(
                    execution.id
                )
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


async def test_current_baseline_repeated_upgrade_preserves_events(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine, tmp_path)
    execution = await service.submit(_command("baseline-repeat-upgrade"))
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
                "session_id": "baseline-session",
            },
        },
    )
    session_factory = create_session_factory(postgres_engine)
    async with session_factory() as session, session.begin():
        outbox = OutboxEventORM.from_execution_event(event)
        session.add(ExecutionEventORM.from_domain(event))
        session.add(outbox)
        await session.flush()
        outbox_id = outbox.id

    database_url = postgres_engine.url.render_as_string(hide_password=False)
    await asyncio.to_thread(_upgrade_and_check_baseline, database_url)

    async with session_factory() as session:
        migrated_event = await session.get(
            ExecutionEventORM,
            event.id,
        )
        migrated_outbox = await session.scalar(
            select(OutboxEventORM).where(
                OutboxEventORM.execution_event_id == event.id
            )
        )
        revision = await session.scalar(
            text("SELECT version_num FROM alembic_version")
        )

        maintenance_count = await session.scalar(
            text("SELECT count(*) FROM executor_maintenance")
        )
        retention_count = await session.scalar(
            text("SELECT count(*) FROM event_retention_lease")
        )

    assert revision == EXPECTED_SCHEMA_REVISION == "0004"
    assert maintenance_count == retention_count == 1
    assert migrated_event is not None
    assert migrated_event.execution_id == execution.id
    assert migrated_event.event_sequence == 1
    assert migrated_event.payload == event.payload
    assert migrated_outbox is not None
    assert migrated_outbox.id == outbox_id
    assert migrated_outbox.payload is None


async def test_current_baseline_downgrade_and_recreate(
    postgres_engine: AsyncEngine,
) -> None:
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    await asyncio.to_thread(_downgrade_baseline, database_url)
    async with postgres_engine.connect() as connection:
        tables = await connection.run_sync(
            lambda sync: set(inspect(sync).get_table_names())
        )
        revision_count = await connection.scalar(
            text("SELECT count(*) FROM alembic_version")
        )
    assert tables == {"alembic_version"}
    assert revision_count == 0

    await asyncio.to_thread(_upgrade_and_check_baseline, database_url)
    async with postgres_engine.connect() as connection:
        tables = await connection.run_sync(
            lambda sync: set(inspect(sync).get_table_names())
        )
        revision = await connection.scalar(
            text("SELECT version_num FROM alembic_version")
        )
        admission = await connection.scalar(
            text("SELECT admission_state FROM executor_maintenance")
        )
        retention_key = await connection.scalar(
            text("SELECT singleton_key FROM event_retention_lease")
        )
    assert tables == set(Base.metadata.tables) | {"alembic_version"}
    assert revision == EXPECTED_SCHEMA_REVISION == "0004"
    assert admission == "ACTIVE"
    assert retention_key == "events"


async def test_background_diagnostics_deduplicate_across_workers(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    service = _service(postgres_engine, tmp_path)
    execution = await service.submit(_command("background-diagnostics"))
    factory = create_session_factory(postgres_engine)
    async with factory() as session, session.begin():
        session.add(_server(capacity=1))
    workers, clients = _workers(postgres_engine, tmp_path, count=1)
    try:
        claim = await workers[0]._claimer.claim(execution.id)
        assert claim is not None
        async with factory() as session, session.begin():
            row = await session.get(ExecutionORM, execution.id)
            assert row is not None
            row.status = ExecutionStatus.WAITING_FOR_OPERATION
            row.runtime_session_id = "waiting-background-kernel"
            row.version += 1
            observation = RuntimeObservation.capture(row)
        outcomes = await asyncio.gather(
            *(
                BackgroundDiagnosticRecorder(factory).record(
                    observation,
                    PermissionError(13, "private"),
                    phase="MULTI_SESSION_PROBE",
                    category=DiagnosticCategory.EXECUTION,
                )
                for _ in range(8)
            )
        )
        assert sum(outcomes) == 1
        items = (
            await SQLAlchemyDiagnosticQueryService(factory).list(execution.id)
        ).items
        assert len(items) == 1 and items[0].attempt_id == claim[2].attempt_id
        async with factory() as session, session.begin():
            row = await observation.current(session, lock=True)
            assert row is not None
            row.fencing_token += 1
            row.version += 1
        assert not await BackgroundDiagnosticRecorder(factory).record(
            observation,
            PermissionError(13, "private"),
            phase="MULTI_SESSION_PROBE",
            category=DiagnosticCategory.EXECUTION,
        )
    finally:
        await _close_redis(clients)


async def test_expired_cleanup_is_reserved_once_across_workers(
    postgres_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(postgres_engine, tmp_path)
    execution = await service.submit(_command("reserve-expired-cleanup"))
    factory = create_session_factory(postgres_engine)
    async with factory() as session, session.begin():
        session.add(_server(capacity=1))
    workers, clients = _workers(postgres_engine, tmp_path, count=2)
    started, release = asyncio.Event(), asyncio.Event()
    deleted: list[str] = []

    class Driver:
        def __init__(self, *_args, **_kwargs):
            pass

        async def delete_session(self, session_id):
            deleted.append(session_id)
            started.set()
            await release.wait()

        async def close(self):
            pass

    monkeypatch.setattr(
        "executor_service.infrastructure.runtime_drivers.JupyterRuntimeDriver",
        Driver,
    )
    task = None
    try:
        claim = await workers[0]._claimer.claim(execution.id)
        assert claim is not None
        async with factory() as session, session.begin():
            row = await session.get(ExecutionORM, execution.id)
            attempt = await session.get(
                ExecutionAttemptORM, claim[2].attempt_id
            )
            assert row is not None and attempt is not None
            row.status = ExecutionStatus.FAILED
            row.runtime_session_id = "expired-reserved-kernel"
            row.retry_strategy = RetryStrategy.FROM_FAILED_STEP
            row.retry_from_sequence = 0
            row.retained_runtime_session_until = utc_now() - timedelta(
                seconds=1
            )
            attempt.status = AttemptStatus.FAILED
            attempt.runtime_session_id = row.runtime_session_id
        task = asyncio.create_task(
            workers[0]._retained_session_cleaner.reconcile()
        )
        async with asyncio.timeout(5):
            await started.wait()
            await workers[1]._retained_session_cleaner.reconcile()
        async with factory() as session:
            row = await session.get(ExecutionORM, execution.id)
            assert (
                row is not None
                and row.retry_strategy == RetryStrategy.NOT_RETRYABLE
            )
            assert (
                row.runtime_session_cleanup_status
                == RuntimeSessionCleanupStatus.PENDING
            )
        assert deleted == ["expired-reserved-kernel"]
        release.set()
        await task
        async with factory() as session:
            row = await session.get(ExecutionORM, execution.id)
            assert row is not None and row.runtime_session_id is None
            assert (
                row.runtime_session_cleanup_status
                == RuntimeSessionCleanupStatus.SUCCEEDED
            )
    finally:
        release.set()
        if task is not None:
            await task
        await _close_redis(clients)


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
