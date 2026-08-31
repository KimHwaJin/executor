"""Fenced cancellation processing for Execution work."""

import asyncio
import logging
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.diagnostics import DiagnosticCategory
from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    OperationStatus,
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimeSessionCleanupStatus,
    StepStatus,
)
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import RuntimeDriverError
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.diagnostic_store import DiagnosticRecorder
from executor_service.infrastructure.execution_leases import (
    CancellationLease,
    ExecutionLeaseLostError,
    require_active_cancellation_lease,
)
from executor_service.infrastructure.execution_worker.claiming import (
    ExecutionClaimer,
)
from executor_service.infrastructure.execution_worker.event_writer import (
    add_execution_completed_event,
    add_operation_completed_event,
    add_step_history_completed_event,
)
from executor_service.infrastructure.execution_worker.lease_heartbeat import (
    LeaseHeartbeatManager,
)
from executor_service.infrastructure.execution_worker.runtime_calls import (
    RuntimeDriverProvider,
)
from executor_service.infrastructure.execution_worker.runtime_cleanup import (
    best_effort_session_stop,
)
from executor_service.infrastructure.execution_worker.types import (
    CancellationWork,
)
from executor_service.infrastructure.runtime_diagnostics import (
    log_runtime_failure,
)

logger = logging.getLogger(__name__)


class CancellationProcessor:
    """Owns cancellation Claim, Runtime cleanup, and terminal persistence."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        claimer: ExecutionClaimer,
        lease_heartbeat: LeaseHeartbeatManager,
        driver_provider: RuntimeDriverProvider,
    ) -> None:
        self._session_factory = session_factory
        self._claimer = claimer
        self._lease_heartbeat = lease_heartbeat
        self._driver_provider = driver_provider
        self._diagnostics = DiagnosticRecorder(session_factory)

    async def cancel(self, execution_id: UUID) -> None:
        work = await self._claimer.claim_cancellation(execution_id)
        if work is None:
            return
        heartbeat = asyncio.create_task(
            self._lease_heartbeat.run_cancellation(work.lease),
            name=f"cancellation-heartbeat-{execution_id}",
        )
        try:
            cleanup_status = await self._stop_cancelled_runtime(work)
            await self.finalize(work.lease, cleanup_status)
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
            await self._diagnostics.record(
                work.lease,
                RuntimeDriverError("Runtime target is not assigned."),
                phase="CANCELLATION_TARGET",
                category=DiagnosticCategory.CLEANUP,
            )
            return RuntimeSessionCleanupStatus.FAILED
        await self._lease_heartbeat.assert_cancellation(work.lease)
        async with self._session_factory() as session:
            target = await session.get(
                RuntimeTargetORM, work.runtime_target_id
            )
        if target is None:
            await self._diagnostics.record(
                work.lease,
                RuntimeDriverError("Assigned Runtime target is missing."),
                phase="CANCELLATION_TARGET",
                category=DiagnosticCategory.CLEANUP,
            )
            return RuntimeSessionCleanupStatus.FAILED
        try:
            driver = self._driver_provider.create(target)
        except Exception as exc:
            log_runtime_failure(
                logger,
                exc,
                phase="CANCELLATION_DRIVER",
                execution_id=work.lease.execution_id,
                fencing_token=work.lease.fencing_token,
            )
            await self._diagnostics.record(
                work.lease,
                exc,
                phase="CANCELLATION_DRIVER",
                category=DiagnosticCategory.CLEANUP,
            )
            return RuntimeSessionCleanupStatus.FAILED
        try:
            return await best_effort_session_stop(
                driver,
                work.runtime_session_id,
                lease=work.lease,
                diagnostics=self._diagnostics,
            )
        finally:
            await driver.close()

    async def finalize(
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
