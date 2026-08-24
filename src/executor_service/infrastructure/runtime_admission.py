"""Shared durable reservation and observed Runtime admission calculations."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.domain.enums import (
    AttemptStatus,
    ExecutionStatus,
    RetryStrategy,
    RuntimeSessionCleanupStatus,
)
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    RuntimeTargetORM,
)


async def count_runtime_reservations(
    session: AsyncSession,
    target_id: UUID,
    now: datetime,
) -> int:
    """Count distinct Executions that durably consume a Runtime slot."""

    reservation_ids = union(
        select(ExecutionAttemptORM.execution_id).where(
            ExecutionAttemptORM.runtime_target_id == target_id,
            ExecutionAttemptORM.status.in_(
                [AttemptStatus.RUNNING, AttemptStatus.WAITING]
            ),
        ),
        select(ExecutionORM.id).where(
            ExecutionORM.runtime_target_id == target_id,
            ExecutionORM.status.in_(
                [ExecutionStatus.FAILED, ExecutionStatus.QUEUED]
            ),
            ExecutionORM.retry_strategy == RetryStrategy.FROM_FAILED_STEP,
            ExecutionORM.retained_runtime_session_until > now,
            ExecutionORM.runtime_session_id.is_not(None),
        ),
        select(ExecutionORM.id).where(
            ExecutionORM.runtime_target_id == target_id,
            ExecutionORM.runtime_session_id.is_not(None),
            ExecutionORM.runtime_session_cleanup_status.in_(
                [
                    RuntimeSessionCleanupStatus.PENDING,
                    RuntimeSessionCleanupStatus.FAILED,
                ]
            ),
        ),
    ).subquery()
    count = await session.scalar(
        select(func.count()).select_from(reservation_ids)
    )
    return int(count or 0)


def session_count_is_fresh(
    target: RuntimeTargetORM,
    now: datetime,
    max_age_seconds: float,
) -> bool:
    observed_at = target.session_count_observed_at
    if (
        target.active_session_count is None
        or observed_at is None
        or target.last_health_error is not None
    ):
        return False
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return observed_at >= now - timedelta(seconds=max_age_seconds)


def admission_used_count(
    target: RuntimeTargetORM,
    reserved_count: int,
    now: datetime,
    max_age_seconds: float,
) -> int:
    if (
        not session_count_is_fresh(target, now, max_age_seconds)
        or target.active_session_count is None
    ):
        return reserved_count
    return max(reserved_count, target.active_session_count)
