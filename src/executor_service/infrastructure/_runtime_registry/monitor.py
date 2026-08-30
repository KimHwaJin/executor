"""Background health monitoring for enabled Runtime Targets."""

import asyncio
import logging
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                target_ids = await self._enabled_target_ids()
                for target_id in target_ids:
                    try:
                        await self._prober.probe(target_id)
                    except Exception:
                        logger.exception(
                            "Runtime Target health update failed",
                            extra={"target_id": target_id},
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Runtime fleet health monitor failed")
            await asyncio.sleep(self._poll_interval_seconds)

    async def _enabled_target_ids(self) -> list[UUID]:
        async with self._session_factory() as session:
            return list(
                await session.scalars(
                    select(RuntimeTargetORM.id).where(
                        RuntimeTargetORM.enabled.is_(True)
                    )
                )
            )
