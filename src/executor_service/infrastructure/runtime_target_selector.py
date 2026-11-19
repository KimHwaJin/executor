"""Runtime Target admission and load-aware selection."""

import logging

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
    admissible_memory_bytes,
    admission_used_count,
    count_runtime_reservations,
    resource_is_fresh,
)
from executor_service.settings import Settings

logger = logging.getLogger(__name__)

_GIB = 1 << 30
# A Target that admits the Execution, its used slots and, when a fresh
# observation gives it, the memory new kernels may still take.
type _Candidate = tuple[RuntimeTargetORM, int, int | None]


class RuntimeTargetSelector:
    """Selects the eligible Target with the most memory left for new kernels."""

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
        reserve = self._settings.kernel_memory_reserve_bytes(
            execution.runtime_profile
        )
        candidates: list[_Candidate] = []
        full: dict[str, str] = {}
        short: dict[str, str] = {}
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
            if effective_usage >= target.max_concurrent_executions:
                full[target.name] = (
                    f"{effective_usage}/{target.max_concurrent_executions}"
                )
                continue
            # A Target whose observation is stale or reports no memory
            # stays eligible: its memory is unknown, not exhausted.
            memory = (
                await admissible_memory_bytes(session, target, self._settings)
                if resource_is_fresh(
                    target,
                    now,
                    self._settings.runtime_resource_max_age_seconds,
                )
                else None
            )
            if memory is not None and memory < reserve:
                short[target.name] = f"{memory / _GIB:.1f}GiB"
                continue
            candidates.append((target, effective_usage, memory))
        if candidates:
            return min(candidates, key=self._rank)[0], None, {}
        if short:
            return None, StatusReasonCode.MEMORY_LIMIT, short
        if full:
            return None, StatusReasonCode.CAPACITY_FULL, full
        return None, StatusReasonCode.PROFILE_UNSUPPORTED, {}

    def _rank(self, candidate: _Candidate) -> tuple[int, int, float, int, str]:
        """Most free memory first; busy CPU and unknown memory come last."""

        target, usage, memory = candidate
        slots = (usage / target.max_concurrent_executions, usage, target.name)
        if memory is None:
            return 2, 0, *slots
        busy = (
            target.cpu_utilization is not None
            and target.cpu_utilization >= self._settings.runtime_cpu_busy_limit
        )
        return int(busy), -memory, *slots
