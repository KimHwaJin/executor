"""Transactional claiming of Execution and cancellation work."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    FailureType,
    OperationMode,
    OperationStatus,
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
    StepStatus,
)
from executor_service.domain.models import Execution, utc_now
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.execution_leases import (
    CancellationLease,
    ExecutionLease,
    require_active_lease,
)
from executor_service.infrastructure.execution_worker.event_writer import (
    add_execution_completed_event,
)
from executor_service.infrastructure.execution_worker.execution_state import (
    fail_active_operation_without_attempt,
)
from executor_service.infrastructure.execution_worker.target_selector import (
    RuntimeTargetSelector,
)
from executor_service.infrastructure.execution_worker.types import (
    CancellationWork,
)
from executor_service.infrastructure.maintenance import (
    ExecutorMaintenanceService,
)
from executor_service.settings import Settings


class ExecutionClaimer:
    """Acquires fenced execution/cancellation ownership transactionally."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        consumer_name: str,
        target_selector: RuntimeTargetSelector,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._consumer_name = consumer_name
        self._target_selector = target_selector

    async def claim_cancellation(
        self, execution_id: UUID
    ) -> CancellationWork | None:
        now = utc_now()
        lease_expires = now + timedelta(
            seconds=self._settings.execution_lease_seconds
        )
        async with self._session_factory() as session, session.begin():
            execution = await session.scalar(
                select(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .with_for_update()
            )
            if (
                execution is None
                or execution.status != ExecutionStatus.CANCEL_REQUESTED
            ):
                return None
            execution_lease_expiry = execution.lease_expires_at
            if (
                execution.lease_owner is not None
                and execution_lease_expiry is not None
                and _as_utc(execution_lease_expiry) > now
            ):
                return None
            active_expiry = execution.cancellation_lease_expires_at
            has_active_owner = (
                execution.cancellation_lease_owner is not None
                and active_expiry is not None
                and _as_utc(active_expiry) > now
            )
            if (
                has_active_owner
                and execution.cancellation_lease_owner != self._consumer_name
            ):
                return None
            if not has_active_owner:
                execution.fencing_token += 1
                execution.lease_owner = None
                execution.lease_expires_at = None
                execution.heartbeat_at = None
            execution.cancellation_lease_owner = self._consumer_name
            execution.cancellation_lease_expires_at = lease_expires
            execution.cancellation_heartbeat_at = now
            execution.updated_at = now
            return CancellationWork(
                lease=CancellationLease(
                    execution_id=execution.id,
                    owner=self._consumer_name,
                    fencing_token=execution.fencing_token,
                ),
                runtime_target_id=execution.runtime_target_id,
                runtime_session_id=execution.runtime_session_id,
            )

    async def claim(
        self, execution_id: UUID
    ) -> tuple[Execution, RuntimeTargetORM, ExecutionLease] | None:
        now = utc_now()
        lease_expires = now + timedelta(
            seconds=self._settings.execution_lease_seconds
        )
        async with self._session_factory() as session, session.begin():
            execution_row = await session.scalar(
                select(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .options(selectinload(ExecutionORM.steps))
                .with_for_update()
            )
            if execution_row is None or execution_row.status not in {
                ExecutionStatus.QUEUED,
                ExecutionStatus.FINALIZING,
            }:
                return None
            if (
                execution_row.runtime_session_id is None
                and not await ExecutorMaintenanceService.admission_is_active(
                    session, lock=True
                )
            ):
                return None
            operation: ExecutionOperationORM | None = None
            if (
                not execution_row.finalization_requested
                and execution_row.active_operation_id is not None
            ):
                operation = await session.scalar(
                    select(ExecutionOperationORM)
                    .where(
                        ExecutionOperationORM.id
                        == execution_row.active_operation_id,
                        ExecutionOperationORM.execution_id == execution_id,
                        ExecutionOperationORM.status == OperationStatus.QUEUED,
                    )
                    .with_for_update()
                )
                if operation is None:
                    return None
            elif (
                not execution_row.finalization_requested
                and execution_row.retry_count == 0
            ):
                return None
            if (
                execution_row.operation_mode == OperationMode.MULTI
                and execution_row.runtime_session_id is not None
                and execution_row.runtime_target_id is not None
            ):
                waiting_attempt = await session.scalar(
                    select(ExecutionAttemptORM)
                    .where(
                        ExecutionAttemptORM.execution_id == execution_id,
                        ExecutionAttemptORM.status == AttemptStatus.WAITING,
                    )
                    .with_for_update()
                )
                target = await session.scalar(
                    select(RuntimeTargetORM)
                    .where(
                        RuntimeTargetORM.id == execution_row.runtime_target_id
                    )
                    .with_for_update()
                )
                if (
                    waiting_attempt is None
                    or target is None
                    or not target.enabled
                    or target.status == RuntimeTargetStatus.OFFLINE
                    or target.runtime_type != execution_row.runtime_type
                    or target.pool != execution_row.runtime_pool
                    or waiting_attempt.runtime_type
                    != execution_row.runtime_type
                    or waiting_attempt.runtime_profile
                    != execution_row.runtime_profile
                    or execution_row.runtime_profile
                    not in target.supported_profiles
                ):
                    return None
                waiting_attempt.status = AttemptStatus.RUNNING
                fencing_token = execution_row.fencing_token + 1
                waiting_attempt.lease_owner = self._consumer_name
                waiting_attempt.lease_expires_at = lease_expires
                waiting_attempt.heartbeat_at = now
                waiting_attempt.fencing_token = fencing_token
                waiting_attempt.error_message = None
                waiting_attempt.failure_type = None
                waiting_attempt.retry_strategy = RetryStrategy.NOT_RETRYABLE
                waiting_attempt.runtime_session_cleanup_status = (
                    RuntimeSessionCleanupStatus.NOT_REQUIRED
                )
                waiting_attempt.runtime_abort_status = (
                    RuntimeAbortStatus.NOT_REQUIRED
                )
                execution_row.status = ExecutionStatus.RUNNING
                execution_row.lease_owner = self._consumer_name
                execution_row.lease_expires_at = lease_expires
                execution_row.heartbeat_at = now
                execution_row.fencing_token = fencing_token
                execution_row.execution_expires_at = (
                    execution_row.execution_expires_at
                    or (execution_row.started_at or now)
                    + timedelta(
                        seconds=self._settings.execution_max_runtime_seconds
                    )
                )
                execution_row.error_message = None
                execution_row.failure_type = None
                execution_row.runtime_session_cleanup_status = (
                    RuntimeSessionCleanupStatus.NOT_REQUIRED
                )
                execution_row.runtime_abort_status = (
                    RuntimeAbortStatus.NOT_REQUIRED
                )
                execution_row.updated_at = now
                execution_row.version += 1
                if operation is not None:
                    operation.status = OperationStatus.RUNNING
                    operation.execution_attempt_id = waiting_attempt.id
                    operation.started_at = now
                    operation.updated_at = now
                return (
                    execution_row.to_domain(),
                    target,
                    ExecutionLease(
                        execution_id=execution_id,
                        attempt_id=waiting_attempt.id,
                        owner=self._consumer_name,
                        fencing_token=fencing_token,
                    ),
                )
            is_resume = (
                execution_row.retry_count > 0
                and execution_row.retry_strategy
                == RetryStrategy.FROM_FAILED_STEP
                and execution_row.retry_from_sequence is not None
                and execution_row.runtime_session_id is not None
                and execution_row.runtime_target_id is not None
            )
            if is_resume:
                if (
                    execution_row.retained_runtime_session_until is None
                    or _as_utc(execution_row.retained_runtime_session_until)
                    <= now
                ):
                    # The retained-session cleanup loop owns expiry finalization and cleanup.
                    return None
                target = await session.scalar(
                    select(RuntimeTargetORM)
                    .where(
                        RuntimeTargetORM.id == execution_row.runtime_target_id
                    )
                    .with_for_update()
                )
                if (
                    target is None
                    or not target.enabled
                    or target.runtime_type != execution_row.runtime_type
                    or target.pool != execution_row.runtime_pool
                    or execution_row.runtime_profile
                    not in target.supported_profiles
                ):
                    await self._fail_unavailable_retained_retry(
                        session,
                        execution_row,
                        now,
                        "The retained Runtime Target is missing or disabled before retry.",
                    )
                    return None
                if target.status == RuntimeTargetStatus.OFFLINE:
                    # OFFLINE can be temporary. Keep the retry pinned to the original target and
                    # session until health monitoring recovers it or the retention window expires.
                    return None
            if not is_resume:
                target = await self._target_selector.select(
                    session, execution_row
                )
            if target is None:
                return None
            attempt_number = (
                await session.scalar(
                    select(func.count(ExecutionAttemptORM.id)).where(
                        ExecutionAttemptORM.execution_id == execution_id
                    )
                )
                or 0
            ) + 1
            attempt_id = uuid4()
            fencing_token = execution_row.fencing_token + 1
            session.add(
                ExecutionAttemptORM(
                    id=attempt_id,
                    execution_id=execution_id,
                    attempt_number=attempt_number,
                    runtime_type=execution_row.runtime_type,
                    runtime_profile=execution_row.runtime_profile,
                    runtime_target_id=target.id,
                    status=AttemptStatus.RUNNING,
                    lease_owner=self._consumer_name,
                    lease_expires_at=lease_expires,
                    heartbeat_at=now,
                    fencing_token=fencing_token,
                    created_by_type=(
                        execution_row.updated_by_type
                        or execution_row.created_by_type
                    ),
                    created_by=execution_row.updated_by
                    or execution_row.created_by,
                    updated_by_type=(
                        execution_row.updated_by_type
                        or execution_row.created_by_type
                    ),
                    updated_by=execution_row.updated_by
                    or execution_row.created_by,
                    started_at=now,
                )
            )
            if operation is not None:
                operation.status = OperationStatus.RUNNING
                operation.execution_attempt_id = attempt_id
                operation.started_at = now
                operation.updated_at = now
            execution_row.status = ExecutionStatus.RUNNING
            execution_row.runtime_target_id = target.id
            execution_row.lease_owner = self._consumer_name
            execution_row.lease_expires_at = lease_expires
            execution_row.heartbeat_at = now
            execution_row.fencing_token = fencing_token
            started_at = execution_row.started_at or now
            execution_row.started_at = started_at
            execution_row.execution_expires_at = (
                execution_row.execution_expires_at
                or started_at
                + timedelta(
                    seconds=self._settings.execution_max_runtime_seconds
                )
            )
            execution_row.error_message = None
            execution_row.failure_type = None
            if not is_resume:
                execution_row.retained_runtime_session_until = None
            execution_row.runtime_session_cleanup_status = (
                RuntimeSessionCleanupStatus.NOT_REQUIRED
            )
            execution_row.runtime_abort_status = (
                RuntimeAbortStatus.NOT_REQUIRED
            )
            execution_row.updated_at = now
            execution_row.version += 1
            return (
                execution_row.to_domain(),
                target,
                ExecutionLease(
                    execution_id=execution_id,
                    attempt_id=attempt_id,
                    owner=self._consumer_name,
                    fencing_token=fencing_token,
                ),
            )

    async def defer_retained_retry(
        self,
        lease: ExecutionLease,
        target_id: UUID,
        diagnostic: str,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution, attempt = await require_active_lease(session, lease)
            target = await session.scalar(
                select(RuntimeTargetORM)
                .where(RuntimeTargetORM.id == target_id)
                .with_for_update()
            )
            if execution.retry_strategy != RetryStrategy.FROM_FAILED_STEP:
                return
            execution.status = ExecutionStatus.QUEUED
            execution.error_message = "The retained Runtime Target is temporarily unavailable; waiting for recovery."
            execution.failure_type = FailureType.RUNTIME_UNAVAILABLE
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.heartbeat_at = None
            execution.updated_at = now
            execution.version += 1
            attempt.status = AttemptStatus.FAILED
            attempt.lease_owner = None
            attempt.lease_expires_at = None
            attempt.error_message = execution.error_message
            attempt.failure_type = FailureType.RUNTIME_UNAVAILABLE
            attempt.retry_strategy = RetryStrategy.FROM_FAILED_STEP
            attempt.runtime_session_cleanup_status = (
                RuntimeSessionCleanupStatus.NOT_REQUIRED
            )
            attempt.finished_at = now
            if execution.active_operation_id is not None:
                await session.execute(
                    update(ExecutionOperationORM)
                    .where(
                        ExecutionOperationORM.id
                        == execution.active_operation_id,
                        ExecutionOperationORM.status
                        == OperationStatus.RUNNING,
                    )
                    .values(
                        status=OperationStatus.QUEUED,
                        execution_attempt_id=None,
                        error_message=None,
                        started_at=None,
                        finished_at=None,
                        updated_at=now,
                    )
                )
            if target is not None:
                target.status = RuntimeTargetStatus.OFFLINE
                target.last_health_check_at = now
                target.last_health_error = diagnostic[:500]
                target.updated_at = now

    async def _fail_unavailable_retained_retry(
        self,
        session: AsyncSession,
        execution: ExecutionORM,
        now: datetime,
        error_message: str,
    ) -> None:
        execution.status = ExecutionStatus.FAILED
        execution.error_message = error_message
        execution.failure_type = FailureType.RUNTIME_UNAVAILABLE
        execution.finished_at = now
        execution.updated_at = now
        execution.lease_owner = None
        execution.lease_expires_at = None
        execution.heartbeat_at = None
        execution.retry_strategy = RetryStrategy.FROM_START
        execution.retry_from_sequence = 0
        execution.retained_runtime_session_until = None
        execution.runtime_session_cleanup_status = (
            RuntimeSessionCleanupStatus.FAILED
        )
        execution.version += 1
        await session.execute(
            update(ExecutionStepORM)
            .where(
                ExecutionStepORM.execution_id == execution.id,
                ExecutionStepORM.status == StepStatus.PENDING,
            )
            .values(status=StepStatus.SKIPPED, finished_at=now, updated_at=now)
        )
        await fail_active_operation_without_attempt(
            session,
            execution,
            now,
            error_message,
        )
        await add_execution_completed_event(session, execution.id)


def _as_utc(value: datetime) -> datetime:
    """SQLite tests may return timezone-naive values for aware columns."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
