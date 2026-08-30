"""Expired Execution lease fencing and recovery."""

from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
)
from executor_service.infrastructure.execution_worker.event_writer import (
    add_execution_completed_event,
    add_operation_completed_event,
    add_step_history_completed_event,
)
from executor_service.infrastructure.execution_worker.session_recovery import (
    RuntimeSessionRecovery,
)
from executor_service.infrastructure.execution_worker.types import (
    ExpiredLeaseRecovery,
)


class LeaseRecoveryProcessor:
    """Fences expired Workers and cleans their abandoned Runtime sessions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        session_recovery: RuntimeSessionRecovery,
    ) -> None:
        self._session_factory = session_factory
        self._session_recovery = session_recovery

    async def recover(self) -> int:
        recovery = await self.fence_expired_leases()
        await self.cleanup_targets(recovery.cleanup_targets)
        return recovery.execution_count

    async def fence_expired_leases(self) -> ExpiredLeaseRecovery:
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

    async def cleanup_targets(
        self,
        cleanup_targets: tuple[tuple[UUID, UUID | None, UUID, str], ...],
    ) -> None:
        for (
            execution_id,
            attempt_id,
            target_id,
            runtime_session_id,
        ) in cleanup_targets:
            await self._session_recovery.cleanup(
                execution_id, attempt_id, target_id, runtime_session_id
            )
