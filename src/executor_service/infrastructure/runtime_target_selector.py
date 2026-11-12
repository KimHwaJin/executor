"""Runtime Target admission and load-aware selection."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.domain.enums import (
    RuntimeTargetStatus,
    StatusReasonCode,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.runtime_admission import (
    admission_used_count,
    count_runtime_reservations,
)
from executor_service.settings import Settings

logger = logging.getLogger(__name__)


class RuntimeTargetSelector:
    """Selects an eligible Target using reservations and fresh utilization."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Why each (pool, type, profile) last found no Target, so a wait is
        # logged when it starts and ends rather than on every pass.
        self._blocked: dict[tuple[str, str, str], StatusReasonCode] = {}

    async def select(
        self, session: AsyncSession, execution: ExecutionORM
    ) -> RuntimeTargetORM | None:
        # Choose from an unlocked read, then lock only the chosen Target and
        # check it again. Locking every candidate made concurrent claims skip
        # the whole pool, so a burst started about one Execution per pass.
        chosen, reason, detail = await self.verdict(session, execution)
        self._report(execution, reason, detail)
        if chosen is None:
            return None
        target = await session.scalar(
            self._eligible(execution)
            .where(RuntimeTargetORM.id == chosen.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if target is None:
            return None
        return (await self._best(session, execution, [target]))[0]

    async def verdict(
        self, session: AsyncSession, execution: ExecutionORM
    ) -> tuple[
        RuntimeTargetORM | None, StatusReasonCode | None, dict[str, str]
    ]:
        """The Target a new run would get, or why none admits it now."""
        targets = list(
            await session.scalars(
                self._eligible(execution).order_by(RuntimeTargetORM.name)
            )
        )
        return await self._best(session, execution, targets)

    def _report(
        self,
        execution: ExecutionORM,
        reason: StatusReasonCode | None,
        detail: dict[str, str],
    ) -> None:
        key = (
            execution.runtime_pool.value,
            execution.runtime_type.value,
            execution.runtime_profile,
        )
        previous = self._blocked.get(key)
        if reason == previous:
            return
        if reason is None:
            del self._blocked[key]
            logger.info(
                "Runtime admission resumed pool=%s runtime_type=%s profile=%s",
                *key,
            )
            return
        self._blocked[key] = reason
        logger.warning(
            "Runtime admission blocked pool=%s runtime_type=%s profile=%s "
            "reason=%s targets=%s",
            *key,
            reason,
            ",".join(f"{name}={value}" for name, value in detail.items())
            or "-",
        )

    @staticmethod
    def _eligible(execution: ExecutionORM) -> Select[tuple[RuntimeTargetORM]]:
        return select(RuntimeTargetORM).where(
            RuntimeTargetORM.pool == execution.runtime_pool,
            RuntimeTargetORM.runtime_type == execution.runtime_type,
            RuntimeTargetORM.enabled.is_(True),
            RuntimeTargetORM.status == RuntimeTargetStatus.ACTIVE,
        )

    async def _best(
        self,
        session: AsyncSession,
        execution: ExecutionORM,
        targets: list[RuntimeTargetORM],
    ) -> tuple[
        RuntimeTargetORM | None, StatusReasonCode | None, dict[str, str]
    ]:
        """The chosen Target, or None with the reason and per-Target values."""

        if not targets:
            return None, StatusReasonCode.NO_ACTIVE_TARGET, {}
        candidates: list[tuple[RuntimeTargetORM, int]] = []
        full: dict[str, str] = {}
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
            else:
                full[target.name] = (
                    f"{effective_usage}/{target.max_concurrent_executions}"
                )
        if not candidates:
            if full:
                return None, StatusReasonCode.CAPACITY_FULL, full
            return None, StatusReasonCode.PROFILE_UNSUPPORTED, {}

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
                return (
                    None,
                    StatusReasonCode.MEMORY_LIMIT,
                    {
                        target.name: f"{target.memory_utilization:.2f}"
                        for target, _usage in fresh_candidates
                    },
                )
            return min(admitted, key=self._resource_candidate_key)[0], None, {}

        best = min(
            candidates,
            key=lambda candidate: (
                candidate[1] / candidate[0].max_concurrent_executions,
                candidate[1],
                candidate[0].name,
            ),
        )[0]
        return best, None, {}

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
