"""Application dependency container and process lifecycle."""

import asyncio
from functools import partial

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.artifact_content import (
    ArtifactContentService,
)
from executor_service.application.diagnostics import DiagnosticQueryService
from executor_service.application.execution_results import (
    ExecutionResultQueryService,
)
from executor_service.application.notebook_queries import (
    ExecutionNotebookQueryService,
)
from executor_service.application.services import ExecutionService
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.migrations import upgrade_database
from executor_service.infrastructure.db.repositories import (
    SQLAlchemyUnitOfWork,
)
from executor_service.infrastructure.db.session import (
    create_engine,
    create_session_factory,
)
from executor_service.infrastructure.diagnostic_store import (
    SQLAlchemyDiagnosticQueryService,
)
from executor_service.infrastructure.event_retention import (
    EventRetentionManager,
)
from executor_service.infrastructure.execution_queries import (
    SQLAlchemyExecutionQueryService,
)
from executor_service.infrastructure.execution_worker import ExecutionWorker
from executor_service.infrastructure.maintenance import (
    ExecutorMaintenanceService,
)
from executor_service.infrastructure.maintenance_runs import (
    MaintenanceRunService,
)
from executor_service.infrastructure.materialized_artifacts import (
    MaterializedArtifactService,
)
from executor_service.infrastructure.outbox import OutboxPublisher
from executor_service.infrastructure.redis_client import create_redis_client
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
)
from executor_service.infrastructure.runtime_adapters import (
    RuntimeAdapterRegistry,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.infrastructure.runtime_storage import (
    FleetRuntimeStorageAccess,
)
from executor_service.interfaces.execution_specs import ExecutionSpecResolver
from executor_service.settings import Settings

EXPECTED_SCHEMA_REVISION = "0013"
READINESS_DATABASE_TIMEOUT_SECONDS = 2
REDIS_PING_TIMEOUT_SECONDS = 1


class ApplicationContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        engine = partial(
            create_engine,
            settings.database_dsn,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
            statement_timeout_seconds=(
                settings.database_statement_timeout_seconds
            ),
            idle_in_transaction_timeout_seconds=(
                settings.database_idle_in_transaction_timeout_seconds
            ),
        )
        self.engine: AsyncEngine = engine(
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout_seconds=settings.database_pool_timeout_seconds,
            pool_recycle_seconds=settings.database_pool_recycle_seconds,
        )
        # Readiness gets its own connection: a pool busy with Executions is
        # load, not an outage, and must not pull the Pod out of the Service.
        # SQLite (local and tests) has no server to load; an unpooled
        # in-memory engine would even open a different, empty database.
        self.readiness_engine: AsyncEngine = (
            self.engine
            if settings.database_dsn.startswith("sqlite")
            else engine(pooled=False)
        )
        self.session_factory = create_session_factory(self.engine)
        self.redis: Redis = create_redis_client(settings)
        self.result_store = FilesystemExecutionResultStore(
            settings.shared_storage_root
        )
        self.runtime_adapters = RuntimeAdapterRegistry(settings)
        self.execution_service = ExecutionService(
            lambda: SQLAlchemyUnitOfWork(self.session_factory),
            self.runtime_adapters.offerings(),
            self.result_store,
            max_steps_per_operation=(
                settings.execution_max_steps_per_operation
            ),
            max_steps_per_execution=(
                settings.execution_max_steps_per_execution
            ),
            max_runtime_seconds=settings.execution_max_runtime_seconds,
        )
        self.execution_queries = SQLAlchemyExecutionQueryService(
            self.session_factory
        )
        self.diagnostic_queries: DiagnosticQueryService = (
            SQLAlchemyDiagnosticQueryService(self.session_factory)
        )
        self.execution_results = ExecutionResultQueryService(
            self.execution_queries
        )
        self.execution_spec_resolver = ExecutionSpecResolver(
            settings.effective_input_storage_root,
            inline_max_bytes=settings.execution_inline_spec_max_bytes,
            file_max_bytes=settings.execution_file_spec_max_bytes,
            max_steps=settings.execution_max_steps_per_operation,
        )
        self.runtime_registry = RuntimeTargetRegistry(
            self.session_factory, settings
        )
        self.maintenance = ExecutorMaintenanceService(self.session_factory)
        self.maintenance_runs = MaintenanceRunService(
            self.session_factory,
            self.execution_service,
            lease_seconds=settings.execution_lease_seconds,
        )
        self.runtime_storage = FleetRuntimeStorageAccess(
            self.session_factory,
            self.runtime_registry,
            self.runtime_adapters,
        )
        self.artifact_content = ArtifactContentService(
            self.execution_queries, self.runtime_storage
        )
        self.notebook_queries = ExecutionNotebookQueryService(
            self.execution_queries, self.runtime_storage
        )
        self.artifact_manager = ExecutionArtifactManager(self.session_factory)
        self.materialized_artifacts = MaterializedArtifactService(
            self.session_factory,
            self.runtime_storage,
            settings.effective_input_storage_root,
            max_bytes=settings.execution_file_spec_max_bytes,
        )
        self.outbox_publisher = OutboxPublisher(
            session_factory=self.session_factory,
            redis=self.redis,
            event_stream_name=settings.redis_event_stream,
            event_stream_maxlen=settings.redis_event_stream_maxlen,
            poll_interval_seconds=settings.outbox_poll_interval_seconds,
            batch_size=settings.outbox_batch_size,
            publish_timeout_seconds=settings.outbox_publish_timeout_seconds,
            shutdown_timeout_seconds=settings.outbox_shutdown_timeout_seconds,
        )
        self.event_retention = EventRetentionManager(
            self.session_factory,
            self.redis,
            settings,
        )
        self.execution_worker = ExecutionWorker(
            session_factory=self.session_factory,
            settings=settings,
            registry=self.runtime_registry,
            driver_factory=self.runtime_adapters,
            artifact_manager=self.artifact_manager,
            result_store=self.result_store,
            maintenance_runs=self.maintenance_runs,
        )

    async def start(self) -> None:
        if self.settings.db_auto_migrate:
            await upgrade_database(self.engine, self.settings)
        await self.maintenance.initialize()
        self.outbox_publisher.start()
        self.event_retention.start()
        if self.settings.runtime_enabled:
            await self.runtime_registry.start()
            await self.execution_worker.start()

    async def stop(self) -> None:
        await self.execution_worker.stop()
        await self.runtime_registry.stop()
        await self.event_retention.stop()
        await self.outbox_publisher.stop()
        await self.redis.aclose()
        await self.engine.dispose()
        if self.readiness_engine is not self.engine:
            await self.readiness_engine.dispose()

    async def readiness(self) -> dict[str, bool]:
        database_ready = False
        try:
            # Stay inside the probe timeout when PostgreSQL is slow.
            async with asyncio.timeout(READINESS_DATABASE_TIMEOUT_SECONDS):
                async with self.readiness_engine.connect() as connection:
                    revision = await connection.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
            # Pods stay ready while a rolling update moves the schema ahead;
            # migrations that drop columns ship a release after the code
            # stops using them (CONTRIBUTING.md).
            database_ready = (
                isinstance(revision, str)
                and revision.isdigit()
                and int(revision) >= int(EXPECTED_SCHEMA_REVISION)
            )
        except Exception:
            pass
        # Redis only carries Agent events, which the Outbox retries, so a
        # Redis outage must not pull every Pod out of the Service.
        checks = {"postgresql": database_ready}
        if self.settings.runtime_enabled:
            checks["worker_accepting"] = self.execution_worker.accepting_work
        return checks

    async def redis_reachable(self) -> bool:
        try:
            # A silent Redis would otherwise hold the caller for the full
            # socket timeout.
            async with asyncio.timeout(REDIS_PING_TIMEOUT_SECONDS):
                return bool(await self.redis.ping())
        except Exception:
            return False
