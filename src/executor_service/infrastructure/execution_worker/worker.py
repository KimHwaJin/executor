"""Runtime execution worker driven by PostgreSQL state and leases."""

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from executor_service.infrastructure.cluster_passes import (
    run_cluster_pass_loop,
)
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
from executor_service.infrastructure.execution_worker.target_selector import (
    RuntimeTargetSelector,
)
from executor_service.infrastructure.execution_worker.work_admission import (
    WorkAdmissionProcessor,
)
from executor_service.infrastructure.maintenance_runs import (
    MaintenanceRunService,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.infrastructure.workspace import (
    WorkspaceManager,
)
from executor_service.settings import Settings

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = 1


class ExecutionWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        registry: RuntimeTargetRegistry,
        artifact_manager: ExecutionArtifactManager,
        result_store: ExecutionResultStore,
        driver_factory: RuntimeDriverFactory,
        maintenance_runs: MaintenanceRunService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._driver_provider = RuntimeDriverProvider(
            registry,
            driver_factory,
        )
        self._artifacts = artifact_manager
        self._result_store = result_store
        self._maintenance_runs = maintenance_runs
        self._workspace = WorkspaceManager()
        self._notebook_projector = NotebookProjector(
            session_factory,
            self._result_store,
            self._workspace,
            artifact_manager,
        )
        self._step_executor = ExecutionStepExecutor(
            session_factory,
            self._result_store,
        )
        # Lease owner identity: unique per Worker, so never configurable.
        self._worker_id = (
            f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
        )
        self._target_selector = RuntimeTargetSelector(settings)
        self._claimer = ExecutionClaimer(
            session_factory,
            settings,
            self._worker_id,
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
            self._session_recovery,
        )
        self._dispatcher = ExecutionJobDispatcher()
        self._work_admission = WorkAdmissionProcessor(
            session_factory,
            self._dispatcher,
            self._runner,
            self._cancellation,
            worker_id=self._worker_id,
            max_active_jobs=settings.execution_worker_max_active,
        )
        self._multi_lifecycle = MultiLifecycleAuditor(
            session_factory,
            self._driver_provider,
            self._dispatcher,
            self._cancellation,
            self._session_recovery,
            queue_timeout_seconds=settings.execution_queue_timeout_seconds,
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
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def accepting_work(self) -> bool:
        return self._dispatcher.accepting

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
        self._stop_event.clear()
        self._step_executor.set_shutting_down(False)
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
        diagnostics = self._session_recovery.diagnostics
        self._maintenance_loops = [
            self._cluster_pass(
                "lease-recovery",
                self._lease_recovery.recover,
                lambda exc: diagnostics.log_loop_failure(
                    exc, phase="LEASE_RECOVERY_SCAN"
                ),
            ),
            self._cluster_pass(
                "retained-session-cleanup",
                self._retained_session_cleaner.reconcile,
                lambda exc: diagnostics.log_loop_failure(
                    exc, phase="RETAINED_CLEANUP_SCAN"
                ),
            ),
            self._cluster_pass(
                "multi-lifecycle-audit",
                self._multi_lifecycle.audit,
                lambda exc: diagnostics.log_loop_failure(
                    exc, phase="MULTI_LIFECYCLE_SCAN"
                ),
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
            maintenance_runs = self._maintenance_runs
            self._maintenance_loops.append(
                self._cluster_pass(
                    "maintenance-runs",
                    lambda: maintenance_runs.reconcile_once(self._worker_id),
                    lambda _exc: logger.exception(
                        "Maintenance Run reconciliation failed"
                    ),
                )
            )
        self._dispatcher.set_accepting(True)
        self._admission_loops = [
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
            # Running Steps keep running on their kernels; release them to
            # another Worker instead of failing them.
            self._step_executor.set_shutting_down(True)
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
            await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)

    def _cluster_pass(
        self,
        key: str,
        run_pass: Callable[[], Awaitable[object]],
        on_error: Callable[[Exception], None],
    ) -> asyncio.Task[None]:
        # Passes scan shared PostgreSQL state, so one Pod per interval is
        # enough; running them everywhere only multiplies the load.
        return asyncio.create_task(
            run_cluster_pass_loop(
                self._session_factory,
                key=key,
                owner=self._worker_id,
                interval_seconds=self._settings.execution_heartbeat_seconds,
                run_pass=run_pass,
                on_error=on_error,
            ),
            name=key,
        )
