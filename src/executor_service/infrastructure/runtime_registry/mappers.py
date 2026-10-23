"""Runtime persistence-to-view mappings and resource value conversion."""

from collections.abc import Sequence
from datetime import UTC, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.application.runtime_targets import (
    RuntimePoolView,
    RuntimeTargetPurgeView,
    RuntimeTargetView,
)
from executor_service.domain.enums import (
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
)
from executor_service.domain.models import utc_now
from executor_service.domain.runtime import RuntimeResourceObservation
from executor_service.infrastructure.db.models import (
    RuntimeTargetORM,
    RuntimeTargetPurgeORM,
)
from executor_service.infrastructure.runtime_admission import (
    admission_used_count,
    count_runtime_reservations,
    session_count_is_fresh,
)
from executor_service.settings import Settings


async def runtime_target_view(
    session: AsyncSession,
    target: RuntimeTargetORM,
    settings: Settings,
) -> RuntimeTargetView:
    now = utc_now()
    active_execution_count = await count_runtime_reservations(
        session, target.id, now
    )
    session_count_fresh = session_count_is_fresh(
        target,
        now,
        settings.runtime_session_count_max_age_seconds,
    )
    effective_usage = admission_used_count(
        target,
        active_execution_count,
        now,
        settings.runtime_session_count_max_age_seconds,
    )
    resource_fresh = _resource_is_fresh(target, settings)
    resource_pressure_score = None
    if resource_fresh:
        pressure_components = [
            effective_usage / target.max_concurrent_executions,
            *(
                [target.cpu_utilization]
                if target.cpu_utilization is not None
                else []
            ),
            *(
                [target.memory_utilization]
                if target.memory_utilization is not None
                else []
            ),
        ]
        resource_pressure_score = max(pressure_components)
    return RuntimeTargetView(
        id=target.id,
        name=target.name,
        runtime_type=target.runtime_type,
        connection_config=target.connection_config,
        pool=target.pool,
        status=target.status,
        enabled=target.enabled,
        max_concurrent_executions=target.max_concurrent_executions,
        supported_profiles=tuple(target.supported_profiles),
        active_execution_count=active_execution_count,
        active_session_count=target.active_session_count,
        admission_used_count=effective_usage,
        session_count_observed_at=target.session_count_observed_at,
        session_count_fresh=session_count_fresh,
        last_health_check_at=target.last_health_check_at,
        last_health_error=target.last_health_error,
        resource_observed_at=target.resource_observed_at,
        resource_last_check_at=target.resource_last_check_at,
        resource_last_error=target.resource_last_error,
        resource_fresh=resource_fresh,
        resource_source=target.resource_source,
        resource_estimated=target.resource_estimated,
        resource_process_count=target.resource_process_count,
        cpu_used_cores=target.cpu_used_cores,
        cpu_capacity_cores=target.cpu_capacity_cores,
        cpu_utilization=target.cpu_utilization,
        memory_used_bytes=target.memory_used_bytes,
        memory_capacity_bytes=target.memory_capacity_bytes,
        memory_utilization=target.memory_utilization,
        resource_pressure_score=resource_pressure_score,
        resource_errors=tuple(target.resource_errors),
        created_by_type=target.created_by_type,
        created_by=target.created_by,
        updated_by_type=target.updated_by_type,
        updated_by=target.updated_by,
        created_at=target.created_at,
        updated_at=target.updated_at,
    )


def pool_summary(
    runtime_type: RuntimeType,
    pool: RuntimePool,
    pool_views: Sequence[RuntimeTargetView],
) -> RuntimePoolView:
    enabled_views = [view for view in pool_views if view.enabled]
    active_views = [
        view
        for view in enabled_views
        if view.status == RuntimeTargetStatus.ACTIVE
    ]
    health_checks = [
        view.last_health_check_at
        for view in pool_views
        if view.last_health_check_at is not None
    ]
    return RuntimePoolView(
        runtime_type=runtime_type,
        pool=pool,
        target_count=len(pool_views),
        enabled_target_count=len(enabled_views),
        active_target_count=len(active_views),
        draining_target_count=sum(
            view.status == RuntimeTargetStatus.DRAINING for view in pool_views
        ),
        offline_target_count=sum(
            view.status == RuntimeTargetStatus.OFFLINE for view in pool_views
        ),
        configured_capacity=sum(
            view.max_concurrent_executions for view in enabled_views
        ),
        schedulable_capacity=sum(
            view.max_concurrent_executions for view in active_views
        ),
        active_execution_count=sum(
            view.active_execution_count for view in pool_views
        ),
        available_capacity=sum(
            view.available_capacity for view in active_views
        ),
        last_health_check_at=max(health_checks) if health_checks else None,
    )


def purge_view(tombstone: RuntimeTargetPurgeORM) -> RuntimeTargetPurgeView:
    created_at = tombstone.created_at
    updated_at = tombstone.updated_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return RuntimeTargetPurgeView(
        target_id=tombstone.target_id,
        name=tombstone.target_name,
        runtime_type=tombstone.runtime_type,
        connection_config=tombstone.connection_config,
        pool=tombstone.pool,
        created_by_type=tombstone.created_by_type,
        created_by=tombstone.created_by,
        updated_by_type=tombstone.updated_by_type,
        updated_by=tombstone.updated_by,
        created_at=created_at,
        updated_at=updated_at,
    )


def resource_source(resource: RuntimeResourceObservation) -> str | None:
    sources = {
        source
        for source in (resource.cpu.source, resource.memory.source)
        if source
    }
    return ",".join(sorted(sources)) or None


def as_float(value: float | int | None) -> float | None:
    return float(value) if value is not None else None


def as_int(value: float | int | None) -> int | None:
    return int(value) if value is not None else None


def _resource_is_fresh(target: RuntimeTargetORM, settings: Settings) -> bool:
    observed_at = target.resource_observed_at
    if observed_at is None or target.resource_last_error is not None:
        return False
    if target.cpu_utilization is None and target.memory_utilization is None:
        return False
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return observed_at >= utc_now() - timedelta(
        seconds=settings.runtime_resource_max_age_seconds
    )
