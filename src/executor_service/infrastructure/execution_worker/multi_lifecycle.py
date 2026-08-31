"""Audit and timeout handling for retained MULTI executions."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.diagnostics import DiagnosticCategory
from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    FailureType,
    OperationMode,
    RetryStrategy,
    RuntimeSessionCleanupStatus,
)
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import RuntimeDriver
from executor_service.infrastructure.background_diagnostics import (
    RuntimeObservation,
)
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.execution_worker.cancellation import (
    CancellationProcessor,
)
from executor_service.infrastructure.execution_worker.dispatcher import (
    ExecutionJobDispatcher,
)
from executor_service.infrastructure.execution_worker.event_writer import (
    add_execution_completed_event,
)
from executor_service.infrastructure.execution_worker.runtime_calls import (
    RuntimeDriverProvider,
)
from executor_service.infrastructure.execution_worker.session_recovery import (
    RuntimeSessionRecovery,
)


class MultiLifecycleAuditor:
    """Expires waiting MULTI executions and verifies retained sessions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        driver_provider: RuntimeDriverProvider,
        dispatcher: ExecutionJobDispatcher,
        cancellation: CancellationProcessor,
        session_recovery: RuntimeSessionRecovery,
    ) -> None:
        self._session_factory = session_factory
        self._driver_provider = driver_provider
        self._dispatcher = dispatcher
        self._cancellation = cancellation
        self._session_recovery = session_recovery

    async def audit(self) -> None:
        await self._request_expired_execution_cancellations()
        now = utc_now()
        async with self._session_factory() as session:
            waiting = list(
                await session.execute(
                    select(ExecutionORM, RuntimeTargetORM)
                    .outerjoin(
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
            observation = RuntimeObservation.capture(execution)
            try:
                await self._audit_waiting(execution, target, observation, now)
            except Exception as exc:
                await self._session_recovery.diagnostics.record(
                    observation,
                    exc,
                    phase="MULTI_AUDIT",
                    category=DiagnosticCategory.EXECUTION,
                )

    async def _audit_waiting(
        self,
        execution: ExecutionORM,
        target: RuntimeTargetORM | None,
        observation: RuntimeObservation,
        now: datetime,
    ) -> None:
        if (
            execution.execution_expires_at is not None
            and _as_utc(execution.execution_expires_at) <= now
        ):
            await self._fail_waiting_execution(
                observation,
                FailureType.EXECUTION_TIMEOUT,
                "Execution exceeded its maximum runtime while waiting for the Agent.",
            )
            return
        if (
            execution.operation_wait_expires_at is not None
            and _as_utc(execution.operation_wait_expires_at) <= now
        ):
            await self._fail_waiting_execution(
                observation,
                FailureType.OPERATION_WAIT_TIMEOUT,
                "The next Operation was not provided before the wait deadline.",
            )
            return
        if target is None or not target.enabled:
            await self._fail_waiting_execution(
                observation,
                FailureType.RUNTIME_UNAVAILABLE,
                "The assigned Runtime Target is missing or disabled while waiting for the Agent.",
            )
            return
        if execution.runtime_session_id is None:
            await self._fail_waiting_execution(
                observation,
                FailureType.RUNTIME_SESSION_LOST,
                "The retained MULTI Runtime session reference was lost.",
            )
            return
        driver: RuntimeDriver | None = None
        phase = "MULTI_DRIVER_CREATE"
        try:
            driver = self._driver_provider.create(target)
            phase = "MULTI_SESSION_PROBE"
            session_exists = await driver.session_exists(
                execution.runtime_session_id
            )
        except Exception as exc:
            # Keep the existing temporary-outage policy and original deadline.
            await self._session_recovery.diagnostics.record(
                observation,
                exc,
                phase=phase,
                category=DiagnosticCategory.EXECUTION,
            )
            return
        finally:
            if driver is not None:
                try:
                    await driver.close()
                except Exception as exc:
                    await self._session_recovery.diagnostics.record(
                        observation,
                        exc,
                        phase="MULTI_DRIVER_CLOSE",
                        category=DiagnosticCategory.CLEANUP,
                    )
        if not session_exists:
            await self._fail_waiting_execution(
                observation,
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
                self._cancellation.cancel(execution_id),
                replace=True,
            )

    async def _fail_waiting_execution(
        self,
        observation: RuntimeObservation,
        failure_type: FailureType,
        error_message: str,
    ) -> None:
        now = utc_now()
        cleanup_target: RuntimeObservation | None = None
        async with self._session_factory() as session, session.begin():
            execution = await observation.current(session, lock=True)
            if (
                execution is None
                or execution.status != ExecutionStatus.WAITING_FOR_OPERATION
            ):
                return
            attempt = await session.scalar(
                select(ExecutionAttemptORM)
                .where(
                    ExecutionAttemptORM.execution_id
                    == observation.execution_id,
                    ExecutionAttemptORM.status == AttemptStatus.WAITING,
                )
                .with_for_update()
            )
            cleanup_required = (
                failure_type != FailureType.RUNTIME_SESSION_LOST
                and execution.runtime_session_id is not None
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
                cleanup_target = RuntimeObservation.capture(execution)
            await add_execution_completed_event(session, execution.id)
        if cleanup_target is not None:
            await self._session_recovery.cleanup(cleanup_target)


def _as_utc(value: datetime) -> datetime:
    """SQLite tests may return timezone-naive values for aware columns."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
