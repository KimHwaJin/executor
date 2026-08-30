"""Redis-triggered Runtime execution worker with PostgreSQL leases."""

import asyncio
import logging
import os
import socket
from datetime import UTC, datetime, timedelta
from uuid import UUID

from opentelemetry.trace import SpanKind
from redis.asyncio import Redis
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.config import Settings
from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    FailureType,
    OperationMode,
    OperationStatus,
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimeSessionCleanupStatus,
    StepStatus,
)
from executor_service.domain.models import (
    utc_now,
)
from executor_service.domain.results import (
    ExecutionResultStore,
)
from executor_service.domain.runtime import (
    RuntimeDriver,
    RuntimeDriverError,
    RuntimeDriverFactory,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.execution_leases import (
    CancellationLease,
    ExecutionLeaseLostError,
    require_active_cancellation_lease,
)
from executor_service.infrastructure.execution_worker.claiming import (
    ExecutionClaimer,
)
from executor_service.infrastructure.execution_worker.dispatcher import (
    ExecutionJobDispatcher,
)
from executor_service.infrastructure.execution_worker.event_writer import (
    add_execution_completed_event,
    add_operation_completed_event,
    add_step_history_completed_event,
)
from executor_service.infrastructure.execution_worker.execution_state import (
    fail_active_operation_without_attempt,
)
from executor_service.infrastructure.execution_worker.lease_heartbeat import (
    LeaseHeartbeatManager,
)
from executor_service.infrastructure.execution_worker.message_validation import (
    RUN_MESSAGE_TYPES,
)
from executor_service.infrastructure.execution_worker.notebook_projector import (
    NotebookProjector,
)
from executor_service.infrastructure.execution_worker.runner import (
    ExecutionRunner,
)
from executor_service.infrastructure.execution_worker.runtime_cleanup import (
    best_effort_session_stop,
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
from executor_service.infrastructure.execution_worker.types import (
    CancellationWork,
    ExpiredLeaseRecovery,
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
    extract_trace_context,
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
        self._registry = registry
        self._driver_factory = (
            driver_factory or ConfiguredRuntimeDriverFactory(settings)
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
            registry,
            self._driver_factory,
            artifact_manager,
            self._result_store,
            self._workspace,
            self._claimer,
            self._lease_heartbeat,
            self._notebook_projector,
            self._step_executor,
            self._tracing,
        )
        self._stream_consumer = WorkStreamConsumer(
            redis,
            settings,
            self._consumer_name,
            self._tracing,
            self._handle_work_message,
        )
        self._dispatcher = ExecutionJobDispatcher()
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

    def _create_driver(self, target: RuntimeTargetORM) -> RuntimeDriver:
        credential = self._registry.resolve_credential(
            target.credential_ref, target.credential_ciphertext
        )
        return self._driver_factory.create(
            target.runtime_type, target.connection_config, credential
        )

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
            startup_recovery = await self._fence_expired_leases()
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
                    self._cleanup_recovery_targets(
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

    async def _handle_work_message(self, fields: dict[str, str]) -> bool:
        message_type = fields.get("message_type")
        execution_id = UUID(fields["aggregate_id"])
        context = extract_trace_context(fields)
        with self._tracing.span(
            "executor.redis.consume",
            context=context,
            kind=SpanKind.CONSUMER,
            attributes={
                "executor.work.message_type": message_type,
                "executor.execution.id": str(execution_id),
            },
        ):
            if message_type in RUN_MESSAGE_TYPES:
                self._dispatcher.dispatch(
                    execution_id, self._runner.run(execution_id)
                )
            elif message_type == "execution.cancellation_ready":
                self._dispatcher.dispatch(
                    execution_id,
                    self._cancel_execution(execution_id),
                    replace=True,
                )
            else:
                return False
        return True

    async def _reconcile_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                async with self._session_factory() as session:
                    rows = list(
                        await session.execute(
                            select(
                                ExecutionORM.id,
                                ExecutionORM.status,
                                ExecutionORM.traceparent,
                                ExecutionORM.tracestate,
                            )
                            .where(
                                ExecutionORM.status.in_(
                                    [
                                        ExecutionStatus.QUEUED,
                                        ExecutionStatus.FINALIZING,
                                        ExecutionStatus.CANCEL_REQUESTED,
                                    ]
                                )
                            )
                            .order_by(ExecutionORM.created_at)
                            .limit(100)
                        )
                    )
                for execution_id, status, traceparent, tracestate in rows:
                    context = extract_trace_context(
                        {
                            "traceparent": traceparent or "",
                            "tracestate": tracestate or "",
                        }
                    )
                    with self._tracing.span(
                        "executor.reconcile",
                        context=context,
                        attributes={
                            "executor.execution.id": str(execution_id)
                        },
                    ):
                        if status == ExecutionStatus.CANCEL_REQUESTED:
                            self._dispatcher.dispatch(
                                execution_id,
                                self._cancel_execution(execution_id),
                                replace=True,
                            )
                        else:
                            self._dispatcher.dispatch(
                                execution_id, self._runner.run(execution_id)
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Execution reconciliation failed")
            await asyncio.sleep(2)

    async def _lease_recovery_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._recover_expired_leases()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Execution lease recovery failed")
            await asyncio.sleep(self._settings.execution_heartbeat_seconds)

    async def _retained_runtime_session_cleanup_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._cleanup_expired_retained_runtime_sessions()
                await self._retry_unresolved_runtime_session_cleanup()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Retained runtime session cleanup failed")
            await asyncio.sleep(self._settings.execution_heartbeat_seconds)

    async def _multi_lifecycle_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._audit_multi_lifecycle()
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

    async def _cancel_execution(self, execution_id: UUID) -> None:
        work = await self._claimer.claim_cancellation(execution_id)
        if work is None:
            return
        heartbeat = asyncio.create_task(
            self._lease_heartbeat.run_cancellation(work.lease),
            name=f"cancellation-heartbeat-{execution_id}",
        )
        try:
            cleanup_status = await self._stop_cancelled_runtime(work)
            await self._finalize_cancellation(work.lease, cleanup_status)
        except ExecutionLeaseLostError:
            logger.info(
                "Cancellation Worker lost ownership; discarding its result",
                extra={
                    "execution_id": str(execution_id),
                    "fencing_token": work.lease.fencing_token,
                },
            )
        except Exception:
            logger.exception(
                "Cancellation Worker failed; another Worker may recover "
                "after its lease expires",
                extra={"execution_id": str(execution_id)},
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _stop_cancelled_runtime(
        self, work: CancellationWork
    ) -> RuntimeSessionCleanupStatus:
        if work.runtime_session_id is None:
            return RuntimeSessionCleanupStatus.NOT_REQUIRED
        if work.runtime_target_id is None:
            return RuntimeSessionCleanupStatus.FAILED
        await self._lease_heartbeat.assert_cancellation(work.lease)
        async with self._session_factory() as session:
            target = await session.get(
                RuntimeTargetORM, work.runtime_target_id
            )
        if target is None:
            return RuntimeSessionCleanupStatus.FAILED
        try:
            driver = self._create_driver(target)
        except Exception:
            logger.exception(
                "Cancellation could not create the assigned Runtime Driver",
                extra={"execution_id": str(work.lease.execution_id)},
            )
            return RuntimeSessionCleanupStatus.FAILED
        try:
            return await best_effort_session_stop(
                driver, work.runtime_session_id
            )
        finally:
            await driver.close()

    async def _finalize_cancellation(
        self,
        lease: CancellationLease,
        cleanup_status: RuntimeSessionCleanupStatus,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution = await require_active_cancellation_lease(session, lease)
            execution_id = lease.execution_id
            abort_was_pending = (
                execution.runtime_abort_status == RuntimeAbortStatus.PENDING
            )
            running_step_attempts = list(
                (
                    await session.execute(
                        select(
                            ExecutionStepAttemptORM.execution_step_id,
                            ExecutionStepAttemptORM.execution_attempt_id,
                        ).where(
                            ExecutionStepAttemptORM.execution_id
                            == execution_id,
                            ExecutionStepAttemptORM.status
                            == StepStatus.RUNNING,
                        )
                    )
                ).all()
            )
            active_operation_id = execution.active_operation_id
            execution.status = ExecutionStatus.CANCELLED
            execution.finished_at = now
            execution.updated_at = now
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.heartbeat_at = None
            execution.cancellation_lease_owner = None
            execution.cancellation_lease_expires_at = None
            execution.cancellation_heartbeat_at = None
            execution.operation_wait_expires_at = None
            execution.failure_type = None
            execution.retry_strategy = RetryStrategy.NOT_RETRYABLE
            execution.retry_from_sequence = None
            execution.retained_runtime_session_until = None
            execution.runtime_session_cleanup_status = cleanup_status
            if abort_was_pending:
                execution.runtime_abort_status = (
                    RuntimeAbortStatus.SESSION_DELETED
                    if cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED
                    else RuntimeAbortStatus.FAILED
                )
            if cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED:
                execution.runtime_session_id = None
            execution.version += 1
            await session.execute(
                update(ExecutionAttemptORM)
                .where(
                    ExecutionAttemptORM.execution_id == execution_id,
                    ExecutionAttemptORM.status.in_(
                        [AttemptStatus.RUNNING, AttemptStatus.WAITING]
                    ),
                )
                .values(
                    status=AttemptStatus.CANCELLED,
                    failure_type=None,
                    retry_strategy=RetryStrategy.NOT_RETRYABLE,
                    runtime_session_cleanup_status=cleanup_status,
                    lease_owner=None,
                    lease_expires_at=None,
                    **(
                        {
                            "runtime_abort_status": (
                                RuntimeAbortStatus.SESSION_DELETED
                                if cleanup_status
                                == RuntimeSessionCleanupStatus.SUCCEEDED
                                else RuntimeAbortStatus.FAILED
                            )
                        }
                        if abort_was_pending
                        else {}
                    ),
                    finished_at=now,
                )
            )
            await session.execute(
                update(ExecutionStepORM)
                .where(
                    ExecutionStepORM.execution_id == execution_id,
                    ExecutionStepORM.status.in_(
                        [StepStatus.PENDING, StepStatus.RUNNING]
                    ),
                )
                .values(
                    status=StepStatus.CANCELLED,
                    finished_at=now,
                    updated_at=now,
                )
            )
            await session.execute(
                update(ExecutionOperationORM)
                .where(
                    ExecutionOperationORM.execution_id == execution_id,
                    ExecutionOperationORM.status.in_(
                        [OperationStatus.QUEUED, OperationStatus.RUNNING]
                    ),
                )
                .values(
                    status=OperationStatus.CANCELLED,
                    finished_at=now,
                    updated_at=now,
                )
            )
            await session.execute(
                update(ExecutionStepAttemptORM)
                .where(
                    ExecutionStepAttemptORM.execution_id == execution_id,
                    ExecutionStepAttemptORM.status == StepStatus.RUNNING,
                )
                .values(status=StepStatus.CANCELLED, finished_at=now)
            )
            for step_id, attempt_id in running_step_attempts:
                await add_step_history_completed_event(
                    session,
                    execution_id,
                    step_id,
                    attempt_id,
                    StepStatus.CANCELLED,
                    error_message=(
                        execution.cancellation_reason
                        or "Step was cancelled by request."
                    ),
                    retryable=False,
                )
            if active_operation_id is not None:
                await add_operation_completed_event(
                    session, execution_id, active_operation_id
                )
            await add_execution_completed_event(session, execution_id)

    async def _audit_multi_lifecycle(self) -> None:
        await self._request_expired_execution_cancellations()
        now = utc_now()
        async with self._session_factory() as session:
            waiting = list(
                await session.execute(
                    select(ExecutionORM, RuntimeTargetORM)
                    .join(
                        RuntimeTargetORM,
                        RuntimeTargetORM.id == ExecutionORM.runtime_target_id,
                    )
                    .where(
                        ExecutionORM.operation_mode == OperationMode.MULTI,
                        ExecutionORM.status
                        == ExecutionStatus.WAITING_FOR_OPERATION,
                    )
                    .order_by(ExecutionORM.updated_at)
                    .limit(200)
                )
            )
        for execution, target in waiting:
            if (
                execution.execution_expires_at is not None
                and _as_utc(execution.execution_expires_at) <= now
            ):
                await self._fail_waiting_execution(
                    execution.id,
                    execution.runtime_session_id,
                    FailureType.EXECUTION_TIMEOUT,
                    "Execution exceeded its maximum runtime while waiting for the Agent.",
                )
                continue
            if (
                execution.operation_wait_expires_at is not None
                and _as_utc(execution.operation_wait_expires_at) <= now
            ):
                await self._fail_waiting_execution(
                    execution.id,
                    execution.runtime_session_id,
                    FailureType.OPERATION_WAIT_TIMEOUT,
                    "The next Operation was not provided before the wait deadline.",
                )
                continue
            if not target.enabled:
                await self._fail_waiting_execution(
                    execution.id,
                    execution.runtime_session_id,
                    FailureType.RUNTIME_UNAVAILABLE,
                    "The assigned Runtime Target was disabled while waiting for the Agent.",
                )
                continue
            if execution.runtime_session_id is None:
                await self._fail_waiting_execution(
                    execution.id,
                    None,
                    FailureType.RUNTIME_SESSION_LOST,
                    "The retained MULTI Runtime session reference was lost.",
                )
                continue
            driver = self._create_driver(target)
            try:
                session_exists = await driver.session_exists(
                    execution.runtime_session_id
                )
            except RuntimeDriverError:
                # OFFLINE can be temporary. The persisted deadlines remain the terminal guard.
                continue
            finally:
                await driver.close()
            if not session_exists:
                await self._fail_waiting_execution(
                    execution.id,
                    execution.runtime_session_id,
                    FailureType.RUNTIME_SESSION_LOST,
                    "The retained MULTI Runtime session no longer exists.",
                )

    async def _request_expired_execution_cancellations(self) -> None:
        now = utc_now()
        expired_ids: list[UUID] = []
        async with self._session_factory() as session, session.begin():
            expired = list(
                await session.scalars(
                    select(ExecutionORM)
                    .where(
                        ExecutionORM.status.in_(
                            [ExecutionStatus.QUEUED, ExecutionStatus.RUNNING]
                        ),
                        ExecutionORM.execution_expires_at.is_not(None),
                        ExecutionORM.execution_expires_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for execution in expired:
                execution.status = ExecutionStatus.CANCEL_REQUESTED
                execution.cancellation_reason = (
                    "Execution exceeded its maximum runtime."
                )
                execution.operation_wait_expires_at = None
                execution.updated_at = now
                execution.version += 1
                expired_ids.append(execution.id)
        for execution_id in expired_ids:
            self._dispatcher.dispatch(
                execution_id,
                self._cancel_execution(execution_id),
                replace=True,
            )

    async def _fail_waiting_execution(
        self,
        execution_id: UUID,
        expected_runtime_session_id: str | None,
        failure_type: FailureType,
        error_message: str,
    ) -> None:
        now = utc_now()
        cleanup_target: tuple[UUID, UUID | None, UUID, str] | None = None
        async with self._session_factory() as session, session.begin():
            execution = await session.scalar(
                select(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .with_for_update()
            )
            if (
                execution is None
                or execution.status != ExecutionStatus.WAITING_FOR_OPERATION
                or execution.runtime_session_id != expected_runtime_session_id
            ):
                return
            attempt = await session.scalar(
                select(ExecutionAttemptORM)
                .where(
                    ExecutionAttemptORM.execution_id == execution_id,
                    ExecutionAttemptORM.status == AttemptStatus.WAITING,
                )
                .with_for_update()
            )
            cleanup_required = (
                failure_type != FailureType.RUNTIME_SESSION_LOST
                and execution.runtime_session_id is not None
                and execution.runtime_target_id is not None
            )
            cleanup_status = (
                RuntimeSessionCleanupStatus.PENDING
                if cleanup_required
                else RuntimeSessionCleanupStatus.NOT_REQUIRED
            )
            execution.status = ExecutionStatus.FAILED
            execution.error_message = error_message
            execution.failure_type = failure_type
            execution.finished_at = now
            execution.updated_at = now
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.operation_wait_expires_at = None
            execution.retry_strategy = RetryStrategy.NOT_RETRYABLE
            execution.retry_from_sequence = None
            execution.retained_runtime_session_until = None
            execution.runtime_session_cleanup_status = cleanup_status
            execution.recovery_count += 1
            execution.version += 1
            if failure_type == FailureType.RUNTIME_SESSION_LOST:
                execution.runtime_session_id = None
            if attempt is not None:
                attempt.status = AttemptStatus.FAILED
                attempt.lease_owner = None
                attempt.lease_expires_at = None
                attempt.error_message = error_message
                attempt.failure_type = failure_type
                attempt.retry_strategy = RetryStrategy.NOT_RETRYABLE
                attempt.runtime_session_cleanup_status = cleanup_status
                attempt.finished_at = now
            if cleanup_required:
                if (
                    execution.runtime_target_id is None
                    or expected_runtime_session_id is None
                ):
                    raise RuntimeError(
                        "Retained Runtime cleanup target unexpectedly missing."
                    )
                cleanup_target = (
                    execution.id,
                    attempt.id if attempt is not None else None,
                    execution.runtime_target_id,
                    expected_runtime_session_id,
                )
            await add_execution_completed_event(session, execution.id)
        if cleanup_target is not None:
            await self._cleanup_abandoned_session(*cleanup_target)

    async def _recover_expired_leases(self) -> int:
        recovery = await self._fence_expired_leases()
        await self._cleanup_recovery_targets(recovery.cleanup_targets)
        return recovery.execution_count

    async def _fence_expired_leases(self) -> ExpiredLeaseRecovery:
        now = utc_now()
        cleanup_targets: list[tuple[UUID, UUID | None, UUID, str]] = []
        recovered_count = 0
        async with self._session_factory() as session, session.begin():
            expired = list(
                await session.scalars(
                    select(ExecutionORM)
                    .where(
                        ExecutionORM.status == ExecutionStatus.RUNNING,
                        or_(
                            ExecutionORM.lease_owner.is_(None),
                            ExecutionORM.lease_expires_at.is_(None),
                            ExecutionORM.lease_expires_at < now,
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for execution in expired:
                recovered_count += 1
                running_step_attempts = list(
                    await session.execute(
                        select(
                            ExecutionStepAttemptORM.execution_step_id,
                            ExecutionStepAttemptORM.execution_attempt_id,
                        ).where(
                            ExecutionStepAttemptORM.execution_id
                            == execution.id,
                            ExecutionStepAttemptORM.status
                            == StepStatus.RUNNING,
                        )
                    )
                )
                recovery_failure_type = (
                    execution.failure_type
                    if execution.runtime_abort_status
                    == RuntimeAbortStatus.PENDING
                    and execution.failure_type
                    in {
                        FailureType.STEP_TIMEOUT,
                        FailureType.OPERATION_TIMEOUT,
                    }
                    else FailureType.LEASE_EXPIRED
                )
                retry_strategy = (
                    RetryStrategy.NOT_RETRYABLE
                    if execution.operation_mode == OperationMode.MULTI
                    else RetryStrategy.FROM_START
                )
                attempt = await session.scalar(
                    select(ExecutionAttemptORM)
                    .where(
                        ExecutionAttemptORM.execution_id == execution.id,
                        ExecutionAttemptORM.status == AttemptStatus.RUNNING,
                    )
                    .with_for_update()
                )
                if (
                    execution.runtime_target_id is not None
                    and execution.runtime_session_id is not None
                ):
                    cleanup_targets.append(
                        (
                            execution.id,
                            attempt.id if attempt is not None else None,
                            execution.runtime_target_id,
                            execution.runtime_session_id,
                        )
                    )
                execution.status = ExecutionStatus.FAILED
                execution.error_message = (
                    "Worker lease expired while Runtime abort was pending; "
                    "the abandoned session requires cleanup."
                    if recovery_failure_type
                    in {
                        FailureType.STEP_TIMEOUT,
                        FailureType.OPERATION_TIMEOUT,
                    }
                    else "Worker lease expired; execution requires retry."
                )
                execution.failure_type = recovery_failure_type
                execution.finished_at = now
                execution.updated_at = now
                execution.lease_owner = None
                execution.lease_expires_at = None
                execution.heartbeat_at = None
                execution.fencing_token += 1
                execution.retry_strategy = retry_strategy
                execution.retry_from_sequence = (
                    0 if retry_strategy == RetryStrategy.FROM_START else None
                )
                execution.retained_runtime_session_until = None
                execution.recovery_count += 1
                execution.runtime_session_cleanup_status = (
                    RuntimeSessionCleanupStatus.PENDING
                    if cleanup_targets
                    and cleanup_targets[-1][0] == execution.id
                    else RuntimeSessionCleanupStatus.NOT_REQUIRED
                )
                execution.version += 1
                await session.execute(
                    update(ExecutionAttemptORM)
                    .where(
                        ExecutionAttemptORM.execution_id == execution.id,
                        ExecutionAttemptORM.status == AttemptStatus.RUNNING,
                    )
                    .values(
                        status=AttemptStatus.FAILED,
                        lease_owner=None,
                        lease_expires_at=None,
                        error_message=execution.error_message,
                        failure_type=recovery_failure_type,
                        retry_strategy=retry_strategy,
                        runtime_session_cleanup_status=execution.runtime_session_cleanup_status,
                        finished_at=now,
                    )
                )
                await session.execute(
                    update(ExecutionStepORM)
                    .where(
                        ExecutionStepORM.execution_id == execution.id,
                        ExecutionStepORM.status == StepStatus.RUNNING,
                    )
                    .values(
                        status=StepStatus.FAILED,
                        error_message=execution.error_message,
                        finished_at=now,
                        updated_at=now,
                    )
                )
                await session.execute(
                    update(ExecutionStepAttemptORM)
                    .where(
                        ExecutionStepAttemptORM.execution_id == execution.id,
                        ExecutionStepAttemptORM.status == StepStatus.RUNNING,
                    )
                    .values(
                        status=StepStatus.FAILED,
                        error_message=execution.error_message,
                        finished_at=now,
                    )
                )
                for step_id, step_attempt_id in running_step_attempts:
                    await add_step_history_completed_event(
                        session,
                        execution.id,
                        step_id,
                        step_attempt_id,
                        StepStatus.FAILED,
                        error_message=(
                            execution.error_message
                            or "Worker lease expired during Step execution."
                        ),
                        retryable=(
                            retry_strategy != RetryStrategy.NOT_RETRYABLE
                        ),
                    )
                await session.execute(
                    update(ExecutionStepORM)
                    .where(
                        ExecutionStepORM.execution_id == execution.id,
                        ExecutionStepORM.status == StepStatus.PENDING,
                    )
                    .values(
                        status=StepStatus.SKIPPED,
                        finished_at=now,
                        updated_at=now,
                    )
                )
                if execution.active_operation_id is not None:
                    operation = await session.scalar(
                        select(ExecutionOperationORM)
                        .where(
                            ExecutionOperationORM.id
                            == execution.active_operation_id,
                            ExecutionOperationORM.status.in_(
                                [
                                    OperationStatus.QUEUED,
                                    OperationStatus.RUNNING,
                                ]
                            ),
                        )
                        .with_for_update()
                    )
                    if operation is not None:
                        operation.status = OperationStatus.FAILED
                        if attempt is not None:
                            operation.execution_attempt_id = attempt.id
                        operation.error_message = execution.error_message
                        operation.finished_at = now
                        operation.updated_at = now
                        await add_operation_completed_event(
                            session, execution.id, operation.id
                        )
                await add_execution_completed_event(session, execution.id)
        return ExpiredLeaseRecovery(
            execution_count=recovered_count,
            cleanup_targets=tuple(cleanup_targets),
        )

    async def _cleanup_recovery_targets(
        self,
        cleanup_targets: tuple[tuple[UUID, UUID | None, UUID, str], ...],
    ) -> None:
        for (
            execution_id,
            attempt_id,
            target_id,
            runtime_session_id,
        ) in cleanup_targets:
            await self._cleanup_abandoned_session(
                execution_id, attempt_id, target_id, runtime_session_id
            )

    async def _cleanup_abandoned_session(
        self,
        execution_id: UUID,
        attempt_id: UUID | None,
        target_id: UUID,
        runtime_session_id: str,
    ) -> None:
        async with self._session_factory() as session:
            target = await session.get(RuntimeTargetORM, target_id)
        if target is None:
            await self._record_cleanup_result(
                execution_id,
                attempt_id,
                runtime_session_id,
                RuntimeSessionCleanupStatus.FAILED,
            )
            return
        try:
            driver = self._create_driver(target)
        except Exception:
            logger.warning(
                "Abandoned runtime session cleanup could not create a driver",
                extra={"execution_id": str(execution_id)},
                exc_info=True,
            )
            await self._record_cleanup_result(
                execution_id,
                attempt_id,
                runtime_session_id,
                RuntimeSessionCleanupStatus.FAILED,
            )
            return
        try:
            await driver.delete_session(runtime_session_id)
        except Exception:
            logger.warning(
                "Abandoned runtime session cleanup failed",
                extra={"execution_id": str(execution_id)},
            )
            cleanup_status = RuntimeSessionCleanupStatus.FAILED
        else:
            cleanup_status = RuntimeSessionCleanupStatus.SUCCEEDED
        finally:
            await driver.close()
        await self._record_cleanup_result(
            execution_id, attempt_id, runtime_session_id, cleanup_status
        )

    async def _record_cleanup_result(
        self,
        execution_id: UUID,
        attempt_id: UUID | None,
        runtime_session_id: str,
        cleanup_status: RuntimeSessionCleanupStatus,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            execution = await session.scalar(
                select(ExecutionORM)
                .where(
                    ExecutionORM.id == execution_id,
                    ExecutionORM.status.in_(
                        [
                            ExecutionStatus.FAILED,
                            ExecutionStatus.CANCELLED,
                        ]
                    ),
                    ExecutionORM.runtime_session_id == runtime_session_id,
                )
                .with_for_update()
            )
            if execution is None:
                return
            abort_was_pending = (
                execution.runtime_abort_status == RuntimeAbortStatus.PENDING
            )
            if cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED:
                execution.runtime_session_id = None
            execution.runtime_session_cleanup_status = cleanup_status
            if abort_was_pending:
                execution.runtime_abort_status = (
                    RuntimeAbortStatus.SESSION_DELETED
                    if cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED
                    else RuntimeAbortStatus.FAILED
                )
            execution.updated_at = utc_now()
            execution.version += 1
            if attempt_id is not None:
                await session.execute(
                    update(ExecutionAttemptORM)
                    .where(ExecutionAttemptORM.id == attempt_id)
                    .values(
                        runtime_session_cleanup_status=cleanup_status,
                        **(
                            {
                                "runtime_abort_status": (
                                    RuntimeAbortStatus.SESSION_DELETED
                                    if cleanup_status
                                    == RuntimeSessionCleanupStatus.SUCCEEDED
                                    else RuntimeAbortStatus.FAILED
                                )
                            }
                            if abort_was_pending
                            else {}
                        ),
                    )
                )

    async def _retry_unresolved_runtime_session_cleanup(self) -> None:
        now = utc_now()
        retry_before = now - timedelta(
            seconds=self._settings.runtime_cleanup_retry_interval_seconds
        )
        cleanup_targets: list[tuple[UUID, UUID | None, UUID | None, str]] = []
        async with self._session_factory() as session, session.begin():
            executions = list(
                await session.scalars(
                    select(ExecutionORM)
                    .where(
                        ExecutionORM.status.in_(
                            [
                                ExecutionStatus.FAILED,
                                ExecutionStatus.CANCELLED,
                            ]
                        ),
                        ExecutionORM.runtime_session_id.is_not(None),
                        ExecutionORM.runtime_session_cleanup_status.in_(
                            [
                                RuntimeSessionCleanupStatus.PENDING,
                                RuntimeSessionCleanupStatus.FAILED,
                            ]
                        ),
                        ExecutionORM.updated_at <= retry_before,
                    )
                    .order_by(ExecutionORM.updated_at)
                    .limit(20)
                    .with_for_update(skip_locked=True)
                )
            )
            for execution in executions:
                runtime_session_id = execution.runtime_session_id
                if runtime_session_id is None:
                    continue
                attempt_id = await session.scalar(
                    select(ExecutionAttemptORM.id)
                    .where(ExecutionAttemptORM.execution_id == execution.id)
                    .order_by(ExecutionAttemptORM.attempt_number.desc())
                    .limit(1)
                )
                execution.runtime_session_cleanup_status = (
                    RuntimeSessionCleanupStatus.PENDING
                )
                execution.updated_at = now
                execution.version += 1
                if attempt_id is not None:
                    await session.execute(
                        update(ExecutionAttemptORM)
                        .where(ExecutionAttemptORM.id == attempt_id)
                        .values(
                            runtime_session_cleanup_status=(
                                RuntimeSessionCleanupStatus.PENDING
                            )
                        )
                    )
                cleanup_targets.append(
                    (
                        execution.id,
                        attempt_id,
                        execution.runtime_target_id,
                        runtime_session_id,
                    )
                )
        for execution_id, attempt_id, target_id, session_id in cleanup_targets:
            if target_id is None:
                await self._record_cleanup_result(
                    execution_id,
                    attempt_id,
                    session_id,
                    RuntimeSessionCleanupStatus.FAILED,
                )
                continue
            await self._cleanup_abandoned_session(
                execution_id,
                attempt_id,
                target_id,
                session_id,
            )

    async def _cleanup_expired_retained_runtime_sessions(self) -> None:
        now = utc_now()
        async with self._session_factory() as session:
            rows = list(
                await session.execute(
                    select(ExecutionORM, RuntimeTargetORM)
                    .join(
                        RuntimeTargetORM,
                        RuntimeTargetORM.id == ExecutionORM.runtime_target_id,
                    )
                    .where(
                        ExecutionORM.status.in_(
                            [ExecutionStatus.FAILED, ExecutionStatus.QUEUED]
                        ),
                        ExecutionORM.retry_strategy
                        == RetryStrategy.FROM_FAILED_STEP,
                        ExecutionORM.retained_runtime_session_until <= now,
                        ExecutionORM.runtime_session_id.is_not(None),
                    )
                )
            )
        for execution, target in rows:
            driver = self._create_driver(target)
            cleanup_status = RuntimeSessionCleanupStatus.SUCCEEDED
            try:
                if execution.runtime_session_id is not None:
                    await driver.delete_session(execution.runtime_session_id)
            except Exception:
                cleanup_status = RuntimeSessionCleanupStatus.FAILED
                logger.warning(
                    "Expired retained runtime session cleanup failed",
                    extra={"execution_id": str(execution.id)},
                )
            finally:
                await driver.close()
            async with (
                self._session_factory() as update_session,
                update_session.begin(),
            ):
                current = await update_session.scalar(
                    select(ExecutionORM)
                    .where(ExecutionORM.id == execution.id)
                    .with_for_update()
                )
                if (
                    current is None
                    or current.status
                    not in {ExecutionStatus.FAILED, ExecutionStatus.QUEUED}
                    or current.retry_strategy != RetryStrategy.FROM_FAILED_STEP
                    or current.retained_runtime_session_until is None
                    or _as_utc(current.retained_runtime_session_until) > now
                ):
                    continue
                retry_was_queued = current.status == ExecutionStatus.QUEUED
                if retry_was_queued:
                    current.status = ExecutionStatus.FAILED
                    current.error_message = (
                        "The retained runtime session retry window expired before "
                        "execution resumed."
                    )
                    current.finished_at = now
                    await update_session.execute(
                        update(ExecutionStepORM)
                        .where(
                            ExecutionStepORM.execution_id == current.id,
                            ExecutionStepORM.status == StepStatus.PENDING,
                        )
                        .values(
                            status=StepStatus.SKIPPED,
                            finished_at=now,
                            updated_at=now,
                        )
                    )
                current.retry_strategy = RetryStrategy.NOT_RETRYABLE
                current.retry_from_sequence = None
                current.retained_runtime_session_until = None
                if cleanup_status == RuntimeSessionCleanupStatus.SUCCEEDED:
                    current.runtime_session_id = None
                current.runtime_session_cleanup_status = cleanup_status
                current.updated_at = now
                current.version += 1
                if retry_was_queued:
                    await fail_active_operation_without_attempt(
                        update_session,
                        current,
                        now,
                        current.error_message
                        or "The retained Runtime session retry window expired.",
                    )
                latest_attempt_id = await update_session.scalar(
                    select(ExecutionAttemptORM.id)
                    .where(ExecutionAttemptORM.execution_id == current.id)
                    .order_by(ExecutionAttemptORM.attempt_number.desc())
                    .limit(1)
                )
                if latest_attempt_id is not None:
                    await update_session.execute(
                        update(ExecutionAttemptORM)
                        .where(ExecutionAttemptORM.id == latest_attempt_id)
                        .values(runtime_session_cleanup_status=cleanup_status)
                    )
                if retry_was_queued:
                    await add_execution_completed_event(
                        update_session, current.id
                    )


def _as_utc(value: datetime) -> datetime:
    """SQLite tests may return timezone-naive values for aware columns."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
