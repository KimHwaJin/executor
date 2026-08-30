"""Shared durable state transitions used by Worker processors."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.domain.enums import OperationStatus
from executor_service.infrastructure.db.models import (
    ExecutionOperationORM,
    ExecutionORM,
)
from executor_service.infrastructure.execution_worker.event_writer import (
    add_operation_completed_event,
)


async def fail_active_operation_without_attempt(
    session: AsyncSession,
    execution: ExecutionORM,
    now: datetime,
    error_message: str,
) -> None:
    """Fail a queued/running Operation when no live Attempt can own it."""
    if execution.active_operation_id is None:
        return
    operation = await session.scalar(
        select(ExecutionOperationORM)
        .where(
            ExecutionOperationORM.id == execution.active_operation_id,
            ExecutionOperationORM.status.in_(
                [OperationStatus.QUEUED, OperationStatus.RUNNING]
            ),
        )
        .with_for_update()
    )
    if operation is None:
        return
    operation.status = OperationStatus.FAILED
    operation.execution_attempt_id = None
    operation.error_message = error_message[:2000]
    operation.finished_at = now
    operation.updated_at = now
    await add_operation_completed_event(session, execution.id, operation.id)
