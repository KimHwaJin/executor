"""Durable state transitions for MULTI Execution Operations."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    OperationStatus,
    StepStatus,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionOperationORM,
    ExecutionStepORM,
)
from executor_service.infrastructure.execution_leases import (
    ExecutionLease,
    require_active_lease,
)
from executor_service.infrastructure.execution_worker.completion_policy import (
    require_completed_results,
)
from executor_service.infrastructure.execution_worker.event_writer import (
    add_operation_completed_event,
)


class MultiOperationState:
    """Completes an Operation while retaining its MULTI Runtime session."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def complete(
        self,
        lease: ExecutionLease,
        operation_id: UUID,
        operation_status: OperationStatus,
        *,
        error_message: str | None = None,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution, attempt = await require_active_lease(session, lease)
            operation = await session.scalar(
                select(ExecutionOperationORM)
                .where(
                    ExecutionOperationORM.id == operation_id,
                    ExecutionOperationORM.execution_id == lease.execution_id,
                )
                .with_for_update()
            )
            if (
                operation is None
                or operation.status != OperationStatus.RUNNING
            ):
                return
            if operation_status == OperationStatus.SUCCEEDED:
                await require_completed_results(
                    session,
                    execution,
                    operation_id,
                    require_notebook_artifact=False,
                )
            execution.status = ExecutionStatus.WAITING_FOR_OPERATION
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.updated_at = now
            execution.finalization_requested = False
            if execution.operation_wait_timeout_seconds is None:
                raise ValueError(
                    "MULTI execution has no Operation wait timeout."
                )
            wait_deadline = now + timedelta(
                seconds=execution.operation_wait_timeout_seconds
            )
            execution.operation_wait_expires_at = min(
                wait_deadline,
                (
                    _as_utc(execution.execution_expires_at)
                    if execution.execution_expires_at is not None
                    else wait_deadline
                ),
            )
            execution.version += 1
            operation.status = operation_status
            operation.error_message = (
                error_message[:2000] if error_message else None
            )
            operation.finished_at = now
            operation.updated_at = now
            attempt.status = AttemptStatus.WAITING
            attempt.lease_owner = None
            attempt.lease_expires_at = None
            await add_operation_completed_event(
                session, lease.execution_id, operation_id
            )

    async def skip_steps_after(
        self,
        lease: ExecutionLease,
        operation_id: UUID,
        failed_sequence: int,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            await require_active_lease(session, lease)
            await session.execute(
                update(ExecutionStepORM)
                .where(
                    ExecutionStepORM.execution_id == lease.execution_id,
                    ExecutionStepORM.operation_id == operation_id,
                    ExecutionStepORM.sequence > failed_sequence,
                    ExecutionStepORM.status == StepStatus.PENDING,
                )
                .values(
                    status=StepStatus.SKIPPED,
                    finished_at=now,
                    updated_at=now,
                )
            )


def _as_utc(value: datetime) -> datetime:
    """SQLite tests may return timezone-naive values for aware columns."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
