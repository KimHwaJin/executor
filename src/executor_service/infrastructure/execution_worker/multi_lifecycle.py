"""Audit and timeout handling for retained MULTI executions."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.diagnostics import DiagnosticCategory
from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    FailureType,
    FinalizedBy,
    OperationMode,
    OperationStatus,
    RetryStrategy,
    RuntimeSessionCleanupStatus,
    StepStatus,
)
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import RuntimeDriver
from executor_service.domain.transitions import require_execution_transition
from executor_service.infrastructure.background_diagnostics import (
    RuntimeObservation,
)
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepORM,
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
from executor_service.infrastructure.execution_worker.execution_state import (
    fail_active_operation_without_attempt,
)
from executor_service.infrastructure.execution_worker.runtime_calls import (
    RuntimeDriverProvider,
)
from executor_service.infrastructure.execution_worker.session_recovery import (
    RuntimeSessionRecovery,
)


class MultiLifecycleAuditor:
    """Applies Execution deadlines and verifies retained MULTI sessions.

    - Never claimed within the queue timeout: FAILED / QUEUE_TIMEOUT.
    - Past the maximum runtime while queued or running: the cancellation
      path stops the Runtime and ends it FAILED / EXECUTION_TIMEOUT.
    - A waiting MULTI past its wait deadline or maximum runtime ends from
      the Executor's view: a successful last Operation is finalized to
      SUCCEEDED, a failed one ends FAILED with that Operation's type.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        driver_provider: RuntimeDriverProvider,
        dispatcher: ExecutionJobDispatcher,
        cancellation: CancellationProcessor,
        session_recovery: RuntimeSessionRecovery,
        *,
        queue_timeout_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self._driver_provider = driver_provider
        self._dispatcher = dispatcher
        self._cancellation = cancellation
        self._session_recovery = session_recovery
        self._queue_timeout = timedelta(seconds=queue_timeout_seconds)

    async def audit(self) -> None:
        await self._fail_queue_timeouts()
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
            and execution.execution_expires_at <= now
        ):
            await self._end_waiting_execution(
                observation, FinalizedBy.MAX_RUNTIME
            )
            return
        if (
            execution.operation_wait_expires_at is not None
            and execution.operation_wait_expires_at <= now
        ):
            await self._end_waiting_execution(
                observation, FinalizedBy.WAIT_TIMEOUT
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

    async def _fail_queue_timeouts(self) -> None:
        """Fail Executions that no Worker claimed within the queue timeout.

        An Execution gets its maximum-runtime deadline on its first claim,
        so a missing deadline means it never started (for example every
        matching Runtime Target stayed offline or full).
        """
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            stale = list(
                await session.scalars(
                    select(ExecutionORM)
                    .where(
                        ExecutionORM.status == ExecutionStatus.QUEUED,
                        ExecutionORM.execution_expires_at.is_(None),
                        ExecutionORM.created_at <= now - self._queue_timeout,
                    )
                    .order_by(ExecutionORM.created_at)
                    .limit(200)
                    .with_for_update(skip_locked=True)
                )
            )
            for execution in stale:
                message = (
                    "No Runtime Target accepted the Execution within "
                    f"{int(self._queue_timeout.total_seconds())} seconds."
                )
                require_execution_transition(
                    execution.status, ExecutionStatus.FAILED
                )
                execution.status = ExecutionStatus.FAILED
                execution.error_message = message
                execution.failure_type = FailureType.QUEUE_TIMEOUT
                execution.finished_at = now
                execution.updated_at = now
                execution.retry_strategy = RetryStrategy.NOT_RETRYABLE
                execution.retry_from_sequence = None
                execution.version += 1
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
                await fail_active_operation_without_attempt(
                    session,
                    execution,
                    now,
                    message,
                    FailureType.QUEUE_TIMEOUT,
                )
                await add_execution_completed_event(session, execution.id)

    async def _request_expired_execution_cancellations(self) -> None:
        """Stop Executions running past their maximum runtime.

        The cancellation path stops the Runtime; ``finalized_by`` set to
        MAX_RUNTIME makes it end FAILED / EXECUTION_TIMEOUT instead of
        CANCELLED. An Execution the auditor already finalized for its
        maximum runtime gets the queue timeout as grace to finish that.
        """
        now = utc_now()
        expired_ids: list[UUID] = []
        async with self._session_factory() as session, session.begin():
            expired = list(
                await session.scalars(
                    select(ExecutionORM)
                    .where(
                        ExecutionORM.status.in_(
                            [
                                ExecutionStatus.QUEUED,
                                ExecutionStatus.RUNNING,
                                ExecutionStatus.FINALIZING,
                            ]
                        ),
                        ExecutionORM.execution_expires_at.is_not(None),
                        or_(
                            and_(
                                or_(
                                    ExecutionORM.finalized_by.is_(None),
                                    ExecutionORM.finalized_by
                                    != FinalizedBy.MAX_RUNTIME,
                                ),
                                ExecutionORM.execution_expires_at <= now,
                            ),
                            and_(
                                ExecutionORM.finalized_by
                                == FinalizedBy.MAX_RUNTIME,
                                ExecutionORM.execution_expires_at
                                <= now - self._queue_timeout,
                            ),
                        ),
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for execution in expired:
                require_execution_transition(
                    execution.status, ExecutionStatus.CANCEL_REQUESTED
                )
                execution.status = ExecutionStatus.CANCEL_REQUESTED
                execution.finalized_by = FinalizedBy.MAX_RUNTIME
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

    async def _end_waiting_execution(
        self,
        observation: RuntimeObservation,
        finalized_by: FinalizedBy,
    ) -> None:
        """End a waiting MULTI from the Executor's view of its last Operation."""
        now = utc_now()
        cleanup_target: RuntimeObservation | None = None
        async with self._session_factory() as session, session.begin():
            execution = await observation.current(session, lock=True)
            if (
                execution is None
                or execution.status != ExecutionStatus.WAITING_FOR_OPERATION
            ):
                return
            operation = (
                await session.get(
                    ExecutionOperationORM, execution.active_operation_id
                )
                if execution.active_operation_id is not None
                else None
            )
            if (
                operation is not None
                and operation.status == OperationStatus.SUCCEEDED
            ):
                # The same state a finalize call leaves; a Worker claims it,
                # projects the notebook and completes it SUCCEEDED.
                require_execution_transition(
                    execution.status, ExecutionStatus.FINALIZING
                )
                execution.status = ExecutionStatus.FINALIZING
                execution.finalization_requested = True
                execution.finalized_by = finalized_by
                execution.operation_wait_expires_at = None
                execution.updated_at = now
                execution.version += 1
                return
            if operation is None or not operation.status.is_terminal:
                failure_type = (
                    FailureType.EXECUTION_TIMEOUT
                    if finalized_by == FinalizedBy.MAX_RUNTIME
                    else FailureType.OPERATION_WAIT_TIMEOUT
                )
                error_message = (
                    "The Execution ended at its "
                    + (
                        "maximum runtime"
                        if finalized_by == FinalizedBy.MAX_RUNTIME
                        else "Operation wait deadline"
                    )
                    + " without a completed Operation."
                )
            else:
                failure_type = operation.failure_type or FailureType.TOOL_ERROR
                error_message = (
                    "No further Operation arrived before the "
                    + (
                        "maximum runtime"
                        if finalized_by == FinalizedBy.MAX_RUNTIME
                        else "wait deadline"
                    )
                    + "; the last Operation failed"
                    + (
                        f": {operation.error_message}"
                        if operation.error_message
                        else "."
                    )
                )[:2000]
            cleanup_target = await self._fail_locked_waiting_execution(
                session,
                execution,
                observation,
                now,
                failure_type,
                error_message,
                finalized_by=finalized_by,
            )
        if cleanup_target is not None:
            await self._session_recovery.cleanup(cleanup_target)

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
            cleanup_target = await self._fail_locked_waiting_execution(
                session,
                execution,
                observation,
                now,
                failure_type,
                error_message,
            )
        if cleanup_target is not None:
            await self._session_recovery.cleanup(cleanup_target)

    async def _fail_locked_waiting_execution(
        self,
        session: AsyncSession,
        execution: ExecutionORM,
        observation: RuntimeObservation,
        now: datetime,
        failure_type: FailureType,
        error_message: str,
        *,
        finalized_by: FinalizedBy | None = None,
    ) -> RuntimeObservation | None:
        attempt = await session.scalar(
            select(ExecutionAttemptORM)
            .where(
                ExecutionAttemptORM.execution_id == observation.execution_id,
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
        require_execution_transition(execution.status, ExecutionStatus.FAILED)
        execution.status = ExecutionStatus.FAILED
        execution.error_message = error_message
        execution.failure_type = failure_type
        execution.finalized_by = finalized_by
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
        await add_execution_completed_event(session, execution.id)
        if cleanup_required:
            return RuntimeObservation.capture(execution)
        return None
