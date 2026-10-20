"""Shared durable reservation and observed Runtime admission calculations."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, Subquery, func, select, union
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


def _reserved_execution_ids(
    now: datetime,
    *,
    target_id: UUID | None = None,
    user_id: str | None = None,
) -> Subquery:
    """Executions that durably hold a Runtime slot, by Target or user."""

    attempts = select(ExecutionAttemptORM.execution_id).where(
        ExecutionAttemptORM.status.in_(
            [AttemptStatus.RUNNING, AttemptStatus.WAITING]
        )
    )
    executions: list[ColumnElement[bool]] = []
    if target_id is not None:
        attempts = attempts.where(
            ExecutionAttemptORM.runtime_target_id == target_id
        )
        executions.append(ExecutionORM.runtime_target_id == target_id)
    if user_id is not None:
        attempts = attempts.join(
            ExecutionORM, ExecutionORM.id == ExecutionAttemptORM.execution_id
        ).where(ExecutionORM.user_id == user_id)
        executions.append(ExecutionORM.user_id == user_id)
    return union(
        attempts,
        select(ExecutionORM.id).where(
            *executions,
            ExecutionORM.status.in_(
                [ExecutionStatus.FAILED, ExecutionStatus.QUEUED]
            ),
            ExecutionORM.retry_strategy == RetryStrategy.FROM_FAILED_STEP,
            ExecutionORM.retained_runtime_session_until > now,
            ExecutionORM.runtime_session_id.is_not(None),
        ),
        select(ExecutionORM.id).where(
            *executions,
            ExecutionORM.runtime_session_id.is_not(None),
            ExecutionORM.runtime_session_cleanup_status.in_(
                [
                    RuntimeSessionCleanupStatus.PENDING,
                    RuntimeSessionCleanupStatus.FAILED,
                ]
            ),
        ),
    ).subquery()


async def count_runtime_reservations(
    session: AsyncSession,
    target_id: UUID,
    now: datetime,
) -> int:
    """Count distinct Executions that durably consume a Runtime slot."""

    reservation_ids = _reserved_execution_ids(now, target_id=target_id)
    count = await session.scalar(
        select(func.count()).select_from(reservation_ids)
    )
    return int(count or 0)


async def sum_reserved_memory_limits(
    session: AsyncSession,
    now: datetime,
    *,
    target_id: UUID | None = None,
    user_id: str | None = None,
) -> int:
    """Total kernel memory limits held by the reserving Executions.

    Executions without a limit count as zero.
    """

    reservation_ids = _reserved_execution_ids(
        now, target_id=target_id, user_id=user_id
    )
    total = await session.scalar(
        select(
            func.coalesce(func.sum(ExecutionORM.memory_limit_bytes), 0)
        ).where(ExecutionORM.id.in_(select(reservation_ids.c[0])))
    )
    return int(total or 0)


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
