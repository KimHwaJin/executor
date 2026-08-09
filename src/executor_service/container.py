"""Application dependency container and process lifecycle."""

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.repositories import SQLAlchemyUnitOfWork
from executor_service.infrastructure.db.session import create_engine, create_session_factory
from executor_service.infrastructure.execution_queries import SQLAlchemyExecutionQueryService
from executor_service.infrastructure.jupyter import JupyterGateway
from executor_service.infrastructure.jupyter_registry import JupyterServerRegistry
from executor_service.infrastructure.outbox import OutboxPublisher
from executor_service.infrastructure.worker import ExecutionWorker

EXPECTED_SCHEMA_REVISION = "0006"


class ApplicationContainer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine: AsyncEngine = create_engine(settings.database_dsn)
        self.session_factory = create_session_factory(self.engine)
        self.redis: Redis = Redis.from_url(settings.redis_dsn, decode_responses=True)
        self.execution_service = ExecutionService(
            lambda: SQLAlchemyUnitOfWork(self.session_factory)
        )
        self.execution_queries = SQLAlchemyExecutionQueryService(self.session_factory)
        self.jupyter_registry = JupyterServerRegistry(self.session_factory, settings)
        self.artifact_manager = ExecutionArtifactManager(self.session_factory, settings)
        self.outbox_publisher = OutboxPublisher(
            session_factory=self.session_factory,
            redis=self.redis,
            stream_name=settings.redis_stream,
            poll_interval_seconds=settings.outbox_poll_interval_seconds,
            batch_size=settings.outbox_batch_size,
        )
        self.execution_worker = ExecutionWorker(
            session_factory=self.session_factory,
            redis=self.redis,
            settings=settings,
            registry=self.jupyter_registry,
            artifact_manager=self.artifact_manager,
        )

    async def start(self) -> None:
        self.outbox_publisher.start()
        if self.settings.jupyter_enabled:
            gateway = JupyterGateway(
                self.settings.jupyter_endpoint,
                self.settings.jupyter_auth_token,
                self.settings.jupyter_request_timeout_seconds,
            )
            try:
                supported_kernels = await gateway.kernel_specs()
            except Exception:
                supported_kernels = ["python3"]
            finally:
                await gateway.close()
            await self.jupyter_registry.ensure_configured_server(supported_kernels)
            await self.jupyter_registry.start()
            await self.execution_worker.start()

    async def stop(self) -> None:
        await self.execution_worker.stop()
        await self.jupyter_registry.stop()
        await self.outbox_publisher.stop()
        await self.redis.aclose()
        await self.engine.dispose()

    async def readiness(self) -> dict[str, bool]:
        database_ready = False
        redis_ready = False
        try:
            async with self.engine.connect() as connection:
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            database_ready = revision == EXPECTED_SCHEMA_REVISION
        except Exception:
            pass
        try:
            redis_ready = bool(await self.redis.ping())
        except Exception:
            pass
        checks = {"postgresql": database_ready, "redis": redis_ready}
        if self.settings.jupyter_enabled:
            try:
                checks["jupyter_fleet"] = await self.jupyter_registry.any_active()
            except Exception:
                checks["jupyter_fleet"] = False
        return checks
