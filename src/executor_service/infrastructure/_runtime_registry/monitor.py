"""Background health monitoring for enabled Runtime Targets."""

import asyncio
import logging
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.models import utc_now
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
        self._task: asyncio.Task[None] | None = None
        self._probes: dict[UUID, asyncio.Task[None]] = {}
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(), name="runtime-fleet-health-monitor"
        )

    async def stop(self) -> None:
        self._stop_event.set()
        tasks = [*self._probes.values()]
        if self._task is not None:
            tasks.append(self._task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._probes.clear()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                # Each Target probes on its own task so one hung Runtime
                # cannot age the observations of the others.
                for target_id in await self._due_target_ids():
                    if target_id not in self._probes:
                        self._probes[target_id] = asyncio.create_task(
                            self._probe(target_id)
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Runtime fleet health monitor failed")
            await asyncio.sleep(self._poll_interval_seconds)

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
        # Every replica runs this monitor; skip Targets another replica
        # checked within the last half interval.
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
