"""Durable completion and abort policy for Runtime execution."""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.config import Settings
from executor_service.domain.diagnostics import DiagnosticCategory
from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    FailureType,
    OperationStatus,
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimeSessionCleanupStatus,
    StepStatus,
)
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import (
    ExecutionCompletionError,
    RuntimeAbortResult,
    RuntimeDriver,
    RuntimeDriverError,
)
from executor_service.infrastructure.db.models import (
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
)
from executor_service.infrastructure.diagnostic_store import DiagnosticRecorder
from executor_service.infrastructure.execution_leases import (
    ExecutionLease,
    ExecutionLeaseLostError,
    require_active_lease,
)
from executor_service.infrastructure.execution_worker.completion_policy import (
    require_completed_results,
)
from executor_service.infrastructure.execution_worker.event_writer import (
    add_execution_completed_event,
    add_operation_completed_event,
    add_step_history_completed_event,
)
from executor_service.infrastructure.execution_worker.types import (
    RuntimeAbortResolution,
    StoredStepFailure,
)
from executor_service.infrastructure.runtime_diagnostics import (
    failure_message,
    log_runtime_failure,
)

logger = logging.getLogger(__name__)


class ExecutionRunFinalizer:
    """Applies fenced abort, release, and terminal state transitions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self.diagnostics = DiagnosticRecorder(session_factory)

    async def record_execution_failure(
        self, lease: ExecutionLease, error: Exception
    ) -> None:
        # Stored Step failures already retain the precise failing phase.
        if not isinstance(error, StoredStepFailure):
            await self.diagnostics.record(
                lease,
                error,
                phase="EXECUTION_RUN",
                category=DiagnosticCategory.EXECUTION,
            )

    async def release_completed_session(
        self,
        lease: ExecutionLease,
        driver: RuntimeDriver,
        runtime_session_id: str,
    ) -> None:
        """A cleanup failure must never authorize replay of successful code."""
        try:
            await driver.delete_session(runtime_session_id)
        except ExecutionLeaseLostError:
            raise
        except Exception as exc:
            await self.diagnostics.record(
                lease,
                exc,
                phase="RUNTIME_RELEASE",
                category=DiagnosticCategory.CLEANUP,
            )
            raise ExecutionCompletionError("RUNTIME_RELEASE") from exc

    async def cancellation_owns_terminal(self, execution_id: UUID) -> bool:
        async with self._session_factory() as session:
            status = await session.scalar(
                select(ExecutionORM.status).where(
                    ExecutionORM.id == execution_id
                )
            )
        return status in {
            ExecutionStatus.CANCEL_REQUESTED,
            ExecutionStatus.CANCELLED,
        }

    async def resolve_runtime_abort(
        self,
        lease: ExecutionLease,
        driver: RuntimeDriver,
        runtime_session_id: str,
        failure_type: FailureType,
    ) -> RuntimeAbortResolution:
        await self._record_runtime_abort(
            lease,
            RuntimeAbortStatus.PENDING,
            RuntimeSessionCleanupStatus.PENDING,
            failure_type,
        )
        try:
            result = await driver.abort_session(
                runtime_session_id,
                self._settings.runtime_abort_timeout_seconds,
            )
        except Exception as exc:
            await self.diagnostics.record(
                lease,
                exc,
                phase="RUNTIME_ABORT",
                category=DiagnosticCategory.CLEANUP,
            )
            log_runtime_failure(
                logger,
                exc,
                phase="RUNTIME_ABORT",
                execution_id=lease.execution_id,
                attempt_id=lease.attempt_id,
                runtime_session_id=runtime_session_id,
            )
            result = RuntimeAbortResult(
                RuntimeAbortStatus.FAILED,
                failure_message(exc),
            )
        if result.status == RuntimeAbortStatus.FAILED:
            await self.diagnostics.record(
                lease,
                RuntimeDriverError(
                    result.message or "Runtime abort failed without a reason."
                ),
                phase="RUNTIME_ABORT_RESULT",
                category=DiagnosticCategory.CLEANUP,
            )
            log_runtime_failure(
                logger,
                RuntimeDriverError(
                    result.message or "Runtime abort failed without a reason."
                ),
                phase="RUNTIME_ABORT_RESULT",
                execution_id=lease.execution_id,
                attempt_id=lease.attempt_id,
                runtime_session_id=runtime_session_id,
            )
        if result.status == RuntimeAbortStatus.IDLE_CONFIRMED:
            resolution = RuntimeAbortResolution(
                abort_status=result.status,
                cleanup_status=RuntimeSessionCleanupStatus.NOT_REQUIRED,
                retry_strategy=RetryStrategy.FROM_FAILED_STEP,
                retain_session=True,
            )
        elif result.status == RuntimeAbortStatus.SESSION_MISSING:
            resolution = RuntimeAbortResolution(
                abort_status=result.status,
                cleanup_status=RuntimeSessionCleanupStatus.SUCCEEDED,
                retry_strategy=RetryStrategy.FROM_START,
                retain_session=False,
            )
        else:
            try:
                await driver.delete_session(runtime_session_id)
            except Exception as exc:
                await self.diagnostics.record(
                    lease,
                    exc,
                    phase="RUNTIME_DELETE_AFTER_ABORT",
                    category=DiagnosticCategory.CLEANUP,
                )
                log_runtime_failure(
                    logger,
                    exc,
                    phase="RUNTIME_DELETE_AFTER_ABORT",
                    execution_id=lease.execution_id,
                    attempt_id=lease.attempt_id,
                    runtime_session_id=runtime_session_id,
                )
                result = RuntimeAbortResult(
                    RuntimeAbortStatus.FAILED,
                    f"{type(exc).__name__}: Runtime session deletion failed.",
                )
                resolution = RuntimeAbortResolution(
                    abort_status=RuntimeAbortStatus.FAILED,
                    cleanup_status=RuntimeSessionCleanupStatus.FAILED,
                    retry_strategy=RetryStrategy.FROM_START,
                    retain_session=False,
                )
            else:
                resolution = RuntimeAbortResolution(
                    abort_status=RuntimeAbortStatus.SESSION_DELETED,
                    cleanup_status=RuntimeSessionCleanupStatus.SUCCEEDED,
                    retry_strategy=RetryStrategy.FROM_START,
                    retain_session=False,
                )
        await self._record_runtime_abort(
            lease,
            resolution.abort_status,
            resolution.cleanup_status,
            failure_type,
        )
        return resolution

    async def _record_runtime_abort(
        self,
        lease: ExecutionLease,
        abort_status: RuntimeAbortStatus,
        cleanup_status: RuntimeSessionCleanupStatus,
        failure_type: FailureType,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution, attempt = await require_active_lease(session, lease)
            execution.runtime_abort_status = abort_status
            execution.runtime_session_cleanup_status = cleanup_status
            execution.failure_type = failure_type
            execution.updated_at = now
            execution.version += 1
            attempt.runtime_abort_status = abort_status
            attempt.runtime_session_cleanup_status = cleanup_status
            attempt.failure_type = failure_type

    async def release_for_cancellation(self, lease: ExecutionLease) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution, attempt = await require_active_lease(
                session,
                lease,
                allowed_statuses=(ExecutionStatus.CANCEL_REQUESTED,),
            )
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.heartbeat_at = None
            execution.updated_at = now
            attempt.lease_owner = None
            attempt.lease_expires_at = None

    async def record_artifact_failure(
        self,
        lease: ExecutionLease,
        sequence: int,
        error: Exception,
    ) -> None:
        await self.diagnostics.record(
            lease,
            error,
            phase="ARTIFACT_REGISTER",
            category=DiagnosticCategory.ARTIFACT,
            sequence=sequence,
        )
        log_runtime_failure(
            logger,
            error,
            phase="ARTIFACT_REGISTER",
            execution_id=lease.execution_id,
            attempt_id=lease.attempt_id,
            sequence=sequence,
        )

    async def finalize(
        self,
        lease: ExecutionLease,
        requested_status: ExecutionStatus,
        error_message: str | None = None,
        *,
        retain_session: bool = False,
        retry_from_sequence: int | None = None,
        failure_type: FailureType | None = None,
        retry_strategy: RetryStrategy = RetryStrategy.NOT_RETRYABLE,
        runtime_session_cleanup_status: RuntimeSessionCleanupStatus = (
            RuntimeSessionCleanupStatus.NOT_REQUIRED
        ),
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution, attempt = await require_active_lease(session, lease)
            if requested_status == ExecutionStatus.SUCCEEDED:
                await require_completed_results(
                    session,
                    execution,
                    execution.active_operation_id,
                    finalizing=execution.finalization_requested,
                )
            running_step_ids = list(
                await session.scalars(
                    select(ExecutionStepAttemptORM.execution_step_id).where(
                        ExecutionStepAttemptORM.execution_attempt_id
                        == lease.attempt_id,
                        ExecutionStepAttemptORM.status == StepStatus.RUNNING,
                    )
                )
            )
            abort_was_pending = (
                execution.runtime_abort_status == RuntimeAbortStatus.PENDING
            )
            if (
                abort_was_pending
                and runtime_session_cleanup_status
                == RuntimeSessionCleanupStatus.NOT_REQUIRED
            ):
                runtime_session_cleanup_status = (
                    RuntimeSessionCleanupStatus.FAILED
                )
            status = requested_status
            attempt_status = AttemptStatus(status.value)
            is_failed = status == ExecutionStatus.FAILED
            effective_failure_type = failure_type if is_failed else None
            effective_retry_strategy = (
                retry_strategy if is_failed else RetryStrategy.NOT_RETRYABLE
            )
            execution.status = status
            execution.error_message = error_message if is_failed else None
            execution.failure_type = effective_failure_type
            execution.finished_at = now
            execution.updated_at = now
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.operation_wait_expires_at = None
            execution.retry_strategy = effective_retry_strategy
            execution.retry_from_sequence = None
            if effective_retry_strategy == RetryStrategy.FROM_FAILED_STEP:
                execution.retry_from_sequence = retry_from_sequence
            elif effective_retry_strategy == RetryStrategy.FROM_START:
                execution.retry_from_sequence = 0
            execution.retained_runtime_session_until = (
                now
                + timedelta(
                    seconds=self._settings.failed_session_retention_seconds
                )
                if is_failed and retain_session
                else None
            )
            execution.runtime_session_cleanup_status = (
                runtime_session_cleanup_status
            )
            if abort_was_pending:
                execution.runtime_abort_status = (
                    RuntimeAbortStatus.SESSION_DELETED
                    if runtime_session_cleanup_status
                    == RuntimeSessionCleanupStatus.SUCCEEDED
                    else RuntimeAbortStatus.FAILED
                )
            if (
                not retain_session
                and runtime_session_cleanup_status
                == RuntimeSessionCleanupStatus.SUCCEEDED
            ):
                execution.runtime_session_id = None
            execution.version += 1
            attempt.status = attempt_status
            attempt.lease_owner = None
            attempt.lease_expires_at = None
            attempt.error_message = error_message if is_failed else None
            attempt.failure_type = effective_failure_type
            attempt.retry_strategy = effective_retry_strategy
            attempt.runtime_session_cleanup_status = (
                runtime_session_cleanup_status
            )
            if abort_was_pending:
                attempt.runtime_abort_status = execution.runtime_abort_status
            attempt.finished_at = now
            completed_operation_id: UUID | None = None
            if execution.active_operation_id is not None:
                operation_update = await session.execute(
                    update(ExecutionOperationORM)
                    .where(
                        ExecutionOperationORM.id
                        == execution.active_operation_id,
                        ExecutionOperationORM.status.in_(
                            [OperationStatus.QUEUED, OperationStatus.RUNNING]
                        ),
                    )
                    .values(
                        status=(
                            OperationStatus.SUCCEEDED
                            if status == ExecutionStatus.SUCCEEDED
                            else OperationStatus.FAILED
                        ),
                        execution_attempt_id=lease.attempt_id,
                        error_message=error_message if is_failed else None,
                        finished_at=now,
                        updated_at=now,
                    )
                )
                operation = await session.scalar(
                    select(ExecutionOperationORM)
                    .where(
                        ExecutionOperationORM.id
                        == execution.active_operation_id
                    )
                    .execution_options(populate_existing=True)
                )
                if (
                    operation is not None
                    and getattr(operation_update, "rowcount", None) == 1
                ):
                    completed_operation_id = operation.id
            if status == ExecutionStatus.FAILED:
                await session.execute(
                    update(ExecutionStepORM)
                    .where(
                        ExecutionStepORM.execution_id == lease.execution_id,
                        ExecutionStepORM.status == StepStatus.RUNNING,
                    )
                    .values(
                        status=StepStatus.FAILED,
                        error_message=error_message,
                        finished_at=now,
                        updated_at=now,
                    )
                )
                for step_id in running_step_ids:
                    await add_step_history_completed_event(
                        session,
                        lease.execution_id,
                        step_id,
                        lease.attempt_id,
                        StepStatus.FAILED,
                        error_message=(
                            error_message or "Step execution failed."
                        ),
                        retryable=(
                            effective_retry_strategy
                            != RetryStrategy.NOT_RETRYABLE
                        ),
                    )
                await session.execute(
                    update(ExecutionStepORM)
                    .where(
                        ExecutionStepORM.execution_id == lease.execution_id,
                        ExecutionStepORM.status == StepStatus.PENDING,
                    )
                    .values(
                        status=StepStatus.SKIPPED,
                        finished_at=now,
                        updated_at=now,
                    )
                )
                await session.execute(
                    update(ExecutionStepAttemptORM)
                    .where(
                        ExecutionStepAttemptORM.execution_attempt_id
                        == lease.attempt_id,
                        ExecutionStepAttemptORM.status == StepStatus.RUNNING,
                    )
                    .values(
                        status=StepStatus.FAILED,
                        error_message=error_message,
                        finished_at=now,
                    )
                )
            if completed_operation_id is not None:
                await add_operation_completed_event(
                    session, lease.execution_id, completed_operation_id
                )
            await add_execution_completed_event(session, lease.execution_id)


def _as_utc(value: datetime) -> datetime:
    """SQLite tests may return timezone-naive values for aware columns."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
