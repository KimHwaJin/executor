"""Runtime Target admission and load-aware selection."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.config import Settings
from executor_service.domain.enums import RuntimeTargetStatus
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.runtime_admission import (
    admission_used_count,
    count_runtime_reservations,
)


class RuntimeTargetSelector:
    """Selects an eligible Target using reservations and fresh utilization."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def select(
        self, session: AsyncSession, execution: ExecutionORM
    ) -> RuntimeTargetORM | None:
        targets = list(
            await session.scalars(
                select(RuntimeTargetORM)
                .where(
                    RuntimeTargetORM.pool == execution.runtime_pool,
                    RuntimeTargetORM.runtime_type == execution.runtime_type,
                    RuntimeTargetORM.enabled.is_(True),
                    RuntimeTargetORM.status == RuntimeTargetStatus.ACTIVE,
                )
                .order_by(RuntimeTargetORM.name)
                .with_for_update(skip_locked=True)
            )
        )
        candidates: list[tuple[RuntimeTargetORM, int]] = []
        now = utc_now()
        for target in targets:
            if execution.runtime_profile not in target.supported_profiles:
                continue
            reserved = await count_runtime_reservations(
                session, target.id, now
            )
            effective_usage = admission_used_count(
                target,
                reserved,
                now,
                self._settings.runtime_session_count_max_age_seconds,
            )
            if effective_usage < target.max_concurrent_executions:
                candidates.append((target, effective_usage))
        if not candidates:
            return None

        fresh_candidates = [
            candidate
            for candidate in candidates
            if self._has_fresh_resource_observation(candidate[0], now)
        ]
        if fresh_candidates:
            admitted = [
                candidate
                for candidate in fresh_candidates
                if candidate[0].memory_utilization is None
                or candidate[0].memory_utilization
                < self._settings.runtime_memory_admission_limit
            ]
            if not admitted:
                return None
            return min(admitted, key=self._resource_candidate_key)[0]

        return min(
            candidates,
            key=lambda candidate: (
                candidate[1] / candidate[0].max_concurrent_executions,
                candidate[1],
                candidate[0].name,
            ),
        )[0]

    def _has_fresh_resource_observation(
        self, target: RuntimeTargetORM, now: datetime
    ) -> bool:
        observed_at = target.resource_observed_at
        if observed_at is None or target.resource_last_error is not None:
            return False
        if (
            target.cpu_utilization is None
            and target.memory_utilization is None
        ):
            return False
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=UTC)
        return observed_at >= now - timedelta(
            seconds=self._settings.runtime_resource_max_age_seconds
        )

    @staticmethod
    def _resource_candidate_key(
        candidate: tuple[RuntimeTargetORM, int],
    ) -> tuple[float, float, int, str]:
        target, reserved = candidate
        pressure = max(
            reserved / target.max_concurrent_executions,
            *(
                value
                for value in (
                    target.cpu_utilization,
                    target.memory_utilization,
                )
                if value is not None
            ),
        )
        memory = (
            target.memory_utilization
            if target.memory_utilization is not None
            else float("inf")
        )
        return pressure, memory, reserved, target.name
