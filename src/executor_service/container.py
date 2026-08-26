"""Application dependency container and process lifecycle."""

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.artifact_content import (
    ArtifactContentService,
)
from executor_service.application.execution_results import (
    ExecutionResultQueryService,
)
from executor_service.application.notebook_queries import (
    ExecutionNotebookQueryService,
)
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import RuntimeType
from executor_service.execution_specs import ExecutionSpecResolver
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
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
from executor_service.infrastructure.execution_queries import (
    SQLAlchemyExecutionQueryService,
)
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
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
)
from executor_service.infrastructure.runtime_drivers import (
    ConfiguredRuntimeDriverFactory,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.infrastructure.runtime_storage import (
    FleetRuntimeStorageAccess,
)
from executor_service.infrastructure.worker import ExecutionWorker
from executor_service.tracing import TracingManager

EXPECTED_SCHEMA_REVISION = "0002"


class ApplicationContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tracing = TracingManager(settings)
        self.engine: AsyncEngine = create_engine(
            settings.database_dsn,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout_seconds=settings.database_pool_timeout_seconds,
            pool_recycle_seconds=settings.database_pool_recycle_seconds,
            connect_timeout_seconds=settings.database_connect_timeout_seconds,
        )
        self.session_factory = create_session_factory(self.engine)
        self.redis: Redis = Redis.from_url(
            settings.redis_dsn, decode_responses=True
        )
        self.result_store = FilesystemExecutionResultStore(
            settings.shared_storage_root
        )
        self.execution_service = ExecutionService(
            lambda: SQLAlchemyUnitOfWork(self.session_factory),
            {RuntimeType.JUPYTER: settings.runtime_allowed_profiles},
            self.result_store,
            max_steps_per_operation=(
                settings.execution_max_steps_per_operation
            ),
            max_steps_per_execution=(
                settings.execution_max_steps_per_execution
            ),
        )
        self.execution_queries = SQLAlchemyExecutionQueryService(
            self.session_factory
        )
        self.execution_results = ExecutionResultQueryService(
            self.execution_queries
        )
        self.execution_spec_resolver = ExecutionSpecResolver(
            settings.request_storage_root,
            inline_max_bytes=settings.execution_inline_spec_max_bytes,
            file_max_bytes=settings.execution_file_spec_max_bytes,
        )
        self.runtime_driver_factory = ConfiguredRuntimeDriverFactory(settings)
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
            self.runtime_driver_factory,
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
            settings.request_storage_root,
            max_bytes=settings.execution_file_spec_max_bytes,
        )
        self.outbox_publisher = OutboxPublisher(
            session_factory=self.session_factory,
            redis=self.redis,
            work_stream_name=settings.redis_work_stream,
            event_stream_name=settings.redis_event_stream,
            poll_interval_seconds=settings.outbox_poll_interval_seconds,
            batch_size=settings.outbox_batch_size,
            tracing=self.tracing,
        )
        self.event_retention = EventRetentionManager(
            self.session_factory,
            self.redis,
            settings,
        )
        self.execution_worker = ExecutionWorker(
            session_factory=self.session_factory,
            redis=self.redis,
            settings=settings,
            registry=self.runtime_registry,
            driver_factory=self.runtime_driver_factory,
            artifact_manager=self.artifact_manager,
            result_store=self.result_store,
            tracing=self.tracing,
            maintenance_runs=self.maintenance_runs,
        )

    async def start(self) -> None:
        await self.maintenance.initialize()
        await self.event_retention.initialize()
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
        await self.tracing.shutdown()
        await self.redis.aclose()
        await self.engine.dispose()

    async def readiness(self) -> dict[str, bool]:
        database_ready = False
        redis_ready = False
        try:
            async with self.engine.connect() as connection:
                revision = await connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                )
            database_ready = revision == EXPECTED_SCHEMA_REVISION
        except Exception:
            pass
        try:
            redis_ready = bool(await self.redis.ping())
        except Exception:
            pass
        checks = {"postgresql": database_ready, "redis": redis_ready}
        if self.settings.runtime_enabled:
            checks["worker_accepting"] = self.execution_worker.accepting_work
        return checks
