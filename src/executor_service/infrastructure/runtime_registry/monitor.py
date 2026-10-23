"""Background health monitoring for enabled Runtime Targets."""

import asyncio
import logging
from datetime import timedelta
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.models import utc_now
from executor_service.infrastructure.cluster_passes import (
    run_cluster_pass_loop,
)
from executor_service.infrastructure.db.models import RuntimeTargetORM

logger = logging.getLogger(__name__)


class RuntimeTargetProbe(Protocol):
    async def probe(self, target_id: UUID) -> object: ...


class RuntimeHealthMonitor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        poll_interval_seconds: float,
        prober: RuntimeTargetProbe,
    ) -> None:
        self._session_factory = session_factory
        self._poll_interval_seconds = poll_interval_seconds
        self._prober = prober
        self._owner = f"runtime-health-{uuid4()}"
        self._task: asyncio.Task[None] | None = None
        self._probes: dict[UUID, asyncio.Task[None]] = {}

    async def start(self) -> None:
        if self._task is not None:
            return
        # Probes write shared Runtime Target state, so one Pod per interval
        # covers the whole fleet.
        self._task = asyncio.create_task(
            run_cluster_pass_loop(
                self._session_factory,
                key="runtime-health",
                owner=self._owner,
                interval_seconds=self._poll_interval_seconds,
                run_pass=self.probe_all,
                on_error=lambda _exc: logger.exception(
                    "Runtime fleet health monitor failed"
                ),
            ),
            name="runtime-fleet-health-monitor",
        )

    async def stop(self) -> None:
        tasks = [*self._probes.values()]
        if self._task is not None:
            tasks.append(self._task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._probes.clear()

    async def probe_all(self) -> None:
        # Each Target probes on its own task so one hung Runtime cannot age
        # the observations of the others.
        for target_id in await self._due_target_ids():
            if target_id not in self._probes:
                self._probes[target_id] = asyncio.create_task(
                    self._probe(target_id)
                )

    async def _probe(self, target_id: UUID) -> None:
        try:
            await self._prober.probe(target_id)
        except Exception:
            logger.exception(
                "Runtime Target health update failed",
                extra={"target_id": target_id},
            )
        finally:
            self._probes.pop(target_id, None)

    async def _due_target_ids(self) -> list[UUID]:
        # A pass that starts just after another Pod's slow probe finished
        # skips Targets checked within the last half interval.
        checked_after = utc_now() - timedelta(
            seconds=self._poll_interval_seconds / 2
        )
        async with self._session_factory() as session:
            return list(
                await session.scalars(
                    select(RuntimeTargetORM.id).where(
                        RuntimeTargetORM.enabled.is_(True),
                        or_(
                            RuntimeTargetORM.last_health_check_at.is_(None),
                            RuntimeTargetORM.last_health_check_at
                            < checked_after,
                        ),
                    )
                )
            )
