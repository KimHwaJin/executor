"""Shared durable reservation and observed Runtime admission calculations."""

from datetime import datetime, timedelta
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
from executor_service.settings import Settings


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


def resource_is_fresh(
    target: RuntimeTargetORM,
    now: datetime,
    max_age_seconds: float,
) -> bool:
    observed_at = target.resource_observed_at
    if observed_at is None or target.resource_last_error is not None:
        return False
    if target.cpu_utilization is None and target.memory_utilization is None:
        return False
    return observed_at >= now - timedelta(seconds=max_age_seconds)


async def admissible_memory_bytes(
    session: AsyncSession,
    target: RuntimeTargetORM,
    settings: Settings,
) -> int | None:
    """Memory new kernels may still take, or None when it is unknown.

    The observed usage misses kernels started after it was taken, so each
    of those counts at its profile's reserve until the next observation.
    """

    if (
        target.memory_used_bytes is None
        or target.memory_capacity_bytes is None
    ):
        return None
    started = await session.execute(
        select(ExecutionAttemptORM.runtime_profile, func.count())
        .where(
            ExecutionAttemptORM.runtime_target_id == target.id,
            ExecutionAttemptORM.status.in_(
                [AttemptStatus.RUNNING, AttemptStatus.WAITING]
            ),
            ExecutionAttemptORM.started_at > target.resource_observed_at,
        )
        .group_by(ExecutionAttemptORM.runtime_profile)
    )
    reserved = sum(
        settings.kernel_memory_reserve_bytes(profile) * count
        for profile, count in started
    )
    return (
        int(
            target.memory_capacity_bytes
            * settings.runtime_memory_admission_limit
        )
        - target.memory_used_bytes
        - reserved
    )
