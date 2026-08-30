"""Redis-triggered Runtime execution worker with PostgreSQL leases."""

import asyncio
import logging
import os
import socket
from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.config import Settings
from executor_service.domain.models import (
    utc_now,
)
from executor_service.domain.results import (
    ExecutionResultStore,
)
from executor_service.domain.runtime import (
    RuntimeDriverFactory,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.execution_worker.cancellation import (
    CancellationProcessor,
)
from executor_service.infrastructure.execution_worker.claiming import (
    ExecutionClaimer,
)
from executor_service.infrastructure.execution_worker.dispatcher import (
    ExecutionJobDispatcher,
)
from executor_service.infrastructure.execution_worker.lease_heartbeat import (
    LeaseHeartbeatManager,
)
from executor_service.infrastructure.execution_worker.lease_recovery import (
    LeaseRecoveryProcessor,
)
from executor_service.infrastructure.execution_worker.multi_lifecycle import (
    MultiLifecycleAuditor,
)
from executor_service.infrastructure.execution_worker.notebook_projector import (
    NotebookProjector,
)
from executor_service.infrastructure.execution_worker.retained_session_cleanup import (
    RetainedSessionCleaner,
)
from executor_service.infrastructure.execution_worker.runner import (
    ExecutionRunner,
)
from executor_service.infrastructure.execution_worker.runtime_calls import (
    RuntimeDriverProvider,
)
from executor_service.infrastructure.execution_worker.session_recovery import (
    RuntimeSessionRecovery,
)
from executor_service.infrastructure.execution_worker.step_executor import (
    ExecutionStepExecutor,
)
from executor_service.infrastructure.execution_worker.stream_consumer import (
    WorkStreamConsumer,
)
from executor_service.infrastructure.execution_worker.target_selector import (
    RuntimeTargetSelector,
)
from executor_service.infrastructure.execution_worker.work_admission import (
    WorkAdmissionProcessor,
)
from executor_service.infrastructure.maintenance_runs import (
    MaintenanceRunService,
)
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
)
from executor_service.infrastructure.runtime_drivers import (
    ConfiguredRuntimeDriverFactory,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.infrastructure.workspace import (
    WorkspaceManager,
)
from executor_service.tracing import (
    TracingManager,
)

logger = logging.getLogger(__name__)


class ExecutionWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
        registry: RuntimeTargetRegistry,
        artifact_manager: ExecutionArtifactManager,
        result_store: ExecutionResultStore | None = None,
        driver_factory: RuntimeDriverFactory | None = None,
        tracing: TracingManager | None = None,
        maintenance_runs: MaintenanceRunService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        resolved_driver_factory = (
            driver_factory or ConfiguredRuntimeDriverFactory(settings)
        )
        self._driver_provider = RuntimeDriverProvider(
            registry,
            resolved_driver_factory,
        )
        self._artifacts = artifact_manager
        self._result_store = result_store or FilesystemExecutionResultStore(
            settings.shared_storage_root
        )
        self._tracing = tracing or TracingManager(settings)
        self._maintenance_runs = maintenance_runs
        self._workspace = WorkspaceManager()
        self._notebook_projector = NotebookProjector(
            session_factory,
            self._result_store,
            self._workspace,
            artifact_manager,
            self._tracing,
        )
        self._step_executor = ExecutionStepExecutor(
            session_factory,
            self._result_store,
        )
        self._consumer_name = settings.execution_consumer_name or (
            f"{socket.gethostname()}-{os.getpid()}"
        )
        self._target_selector = RuntimeTargetSelector(settings)
        self._claimer = ExecutionClaimer(
            session_factory,
            settings,
            self._consumer_name,
            self._target_selector,
        )
        self._lease_heartbeat = LeaseHeartbeatManager(
            session_factory,
            settings,
        )
        self._runner = ExecutionRunner(
            session_factory,
            settings,
            self._driver_provider,
            artifact_manager,
            self._result_store,
            self._workspace,
            self._claimer,
            self._lease_heartbeat,
            self._notebook_projector,
            self._step_executor,
            self._tracing,
        )
        self._cancellation = CancellationProcessor(
            session_factory,
            self._claimer,
            self._lease_heartbeat,
            self._driver_provider,
        )
        self._session_recovery = RuntimeSessionRecovery(
            session_factory,
            self._driver_provider,
        )
        self._lease_recovery = LeaseRecoveryProcessor(
            session_factory,
            self._session_recovery,
        )
        self._retained_session_cleaner = RetainedSessionCleaner(
            session_factory,
            settings,
            self._driver_provider,
            self._session_recovery,
        )
        self._dispatcher = ExecutionJobDispatcher()
        self._work_admission = WorkAdmissionProcessor(
            session_factory,
            self._dispatcher,
            self._runner,
            self._cancellation,
            self._tracing,
        )
        self._stream_consumer = WorkStreamConsumer(
            redis,
            settings,
            self._consumer_name,
            self._tracing,
            self._work_admission.handle_message,
        )
        self._multi_lifecycle = MultiLifecycleAuditor(
            session_factory,
            self._driver_provider,
            self._dispatcher,
            self._cancellation,
            self._session_recovery,
        )
        self._stop_event = asyncio.Event()
        self._admission_loops: list[asyncio.Task[None]] = []
        self._maintenance_loops: list[asyncio.Task[None]] = []
        self._draining = False
        self._stopped = True
        self._startup_reconciliation_completed_at: datetime | None = None
        self._startup_recovered_execution_count = 0
        self._startup_cleanup_target_count = 0

    @property
    def accepting_work(self) -> bool:
        return self._dispatcher.accepting

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def active_job_count(self) -> int:
        return self._dispatcher.active_job_count

    @property
    def startup_reconciliation_completed_at(self) -> datetime | None:
        return self._startup_reconciliation_completed_at

    @property
    def startup_recovered_execution_count(self) -> int:
        return self._startup_recovered_execution_count

    @property
    def startup_cleanup_target_count(self) -> int:
        return self._startup_cleanup_target_count

    @property
    def lifecycle_state(self) -> str:
        if self._stopped:
            return "STOPPED"
        if self._draining:
            return "DRAINING"
        if self.accepting_work:
            return "ACCEPTING"
        return "STARTING"

    async def start(self) -> None:
        if (
            not self._settings.runtime_enabled
            or self._admission_loops
            or self._maintenance_loops
        ):
            return
        await self._stream_consumer.ensure_group()
        self._stop_event.clear()
        self._stopped = False
        self._draining = False
        self._dispatcher.set_accepting(False)
        self._startup_reconciliation_completed_at = None
        self._startup_recovered_execution_count = 0
        self._startup_cleanup_target_count = 0
        try:
            startup_recovery = (
                await self._lease_recovery.fence_expired_leases()
            )
        except Exception:
            self._stopped = True
            logger.exception("Executor startup reconciliation failed")
            raise
        self._startup_reconciliation_completed_at = utc_now()
        self._startup_recovered_execution_count = (
            startup_recovery.execution_count
        )
        self._startup_cleanup_target_count = len(
            startup_recovery.cleanup_targets
        )
        self._maintenance_loops = [
            asyncio.create_task(
                self._lease_recovery_loop(), name="execution-lease-recovery"
            ),
            asyncio.create_task(
                self._retained_runtime_session_cleanup_loop(),
                name="retained-session-cleanup",
            ),
            asyncio.create_task(
                self._multi_lifecycle_loop(),
                name="multi-lifecycle-auditor",
            ),
        ]
        if startup_recovery.cleanup_targets:
            self._maintenance_loops.append(
                asyncio.create_task(
                    self._lease_recovery.cleanup_targets(
                        startup_recovery.cleanup_targets
                    ),
                    name="startup-runtime-session-cleanup",
                )
            )
        if self._maintenance_runs is not None:
            self._maintenance_loops.append(
                asyncio.create_task(
                    self._maintenance_run_loop(),
                    name="maintenance-run-reconciler",
                )
            )
        self._dispatcher.set_accepting(True)
        self._admission_loops = [
            asyncio.create_task(
                self._stream_consumer.run(self._stop_event),
                name="execution-stream-consumer",
            ),
            asyncio.create_task(
                self._stream_consumer.pending_recovery_loop(self._stop_event),
                name="execution-pending-recovery",
            ),
            asyncio.create_task(
                self._reconcile_loop(), name="execution-reconciler"
            ),
        ]

    async def begin_drain(self) -> None:
        if self._stopped or self._draining:
            return
        self._dispatcher.set_accepting(False)
        self._draining = True
        logger.info(
            "Executor Worker drain started",
            extra={"active_job_count": self.active_job_count},
        )
        await self._cancel_tasks(self._admission_loops)

    async def stop(self) -> None:
        if self._stopped and self.active_job_count == 0:
            return
        await self.begin_drain()
        if self.active_job_count:
            try:
                async with asyncio.timeout(
                    self._settings.execution_drain_timeout_seconds
                ):
                    await self._dispatcher.wait_idle()
            except TimeoutError:
                logger.warning(
                    "Executor Worker drain deadline exceeded; cancelling remaining jobs",
                    extra={"active_job_count": self.active_job_count},
                )
        self._stop_event.set()
        await self._cancel_tasks(self._maintenance_loops)
        if self.active_job_count:
            await self._dispatcher.cancel_all()
        self._dispatcher.set_accepting(False)
        self._draining = False
        self._stopped = True
        logger.info("Executor Worker stopped")

    @staticmethod
    async def _cancel_tasks(tasks: list[asyncio.Task[None]]) -> None:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        tasks.clear()

    async def _reconcile_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._work_admission.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Execution reconciliation failed")
            await asyncio.sleep(2)

    async def _lease_recovery_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._lease_recovery.recover()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Execution lease recovery failed")
            await asyncio.sleep(self._settings.execution_heartbeat_seconds)

    async def _retained_runtime_session_cleanup_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._retained_session_cleaner.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Retained runtime session cleanup failed")
            await asyncio.sleep(self._settings.execution_heartbeat_seconds)

    async def _multi_lifecycle_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._multi_lifecycle.audit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MULTI execution lifecycle audit failed")
            await asyncio.sleep(self._settings.execution_heartbeat_seconds)

    async def _maintenance_run_loop(self) -> None:
        if self._maintenance_runs is None:
            return
        while not self._stop_event.is_set():
            try:
                await self._maintenance_runs.reconcile_once(
                    self._consumer_name
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Maintenance Run reconciliation failed")
            await asyncio.sleep(self._settings.execution_heartbeat_seconds)
