"""Bound Redis transport and durable event-history retention."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import (
    ExecutionStatus,
    OutboxStatus,
)
from executor_service.domain.models import utc_now
from executor_service.domain.tenants import tenant_event_stream
from executor_service.infrastructure.cluster_passes import (
    run_cluster_pass_loop,
)
from executor_service.infrastructure.db.models import (
    ExecutionEventORM,
    ExecutionORM,
    OutboxEventORM,
)
from executor_service.infrastructure.redis_streams import (
    MAX_BATCH_SIZE,
    check_redis_compatibility,
    trim_before,
)
from executor_service.settings import Settings

logger = logging.getLogger(__name__)

RETENTION_PASS_KEY = "events"
# Leaves the rest of a large backlog to the next pass.
RETENTION_PASS_BUDGET_SECONDS = 60
_TERMINAL_EXECUTION_STATUSES = (
    ExecutionStatus.SUCCEEDED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
)


@dataclass(frozen=True, slots=True)
class EventRetentionResult:
    published_outbox_deleted: int
    execution_events_deleted: int
    event_stream_trimmed: int


class EventRetentionManager:
    """Run bounded retention passes on one Pod per interval."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._settings = settings
        self._owner = f"retention-{uuid4()}"
        self._redis_checked = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if (
            not self._settings.event_retention_enabled
            or self._task is not None
        ):
            return
        self._task = asyncio.create_task(
            run_cluster_pass_loop(
                self._session_factory,
                key=RETENTION_PASS_KEY,
                owner=self._owner,
                interval_seconds=self._settings.event_retention_interval_seconds,
                run_pass=self.run_pass,
                on_error=lambda _exc: logger.exception(
                    "Event retention pass failed"
                ),
            ),
            name="event-retention",
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def run_pass(self) -> EventRetentionResult:
        """Delete expired rows and entries in batches until done or out of time.

        Each batch commits on its own, so a long backlog never holds locks
        for the whole pass; what the time budget leaves waits for the next.
        """
        deadline = time.monotonic() + RETENTION_PASS_BUDGET_SECONDS
        batch_size = self._settings.event_retention_batch_size
        outbox_deleted = await _drain(
            self._delete_published_outbox, batch_size, deadline
        )
        events_deleted = await _drain(
            self._delete_execution_events, batch_size, deadline
        )
        event_trimmed = 0
        if await self._redis_supports_retention():
            for stream in await self._event_streams():
                event_trimmed += await self._trim_event_stream(
                    stream, deadline
                )
        return EventRetentionResult(
            published_outbox_deleted=outbox_deleted,
            execution_events_deleted=events_deleted,
            event_stream_trimmed=event_trimmed,
        )

    async def _redis_supports_retention(self) -> bool:
        """Check Redis once before trimming; startup never waits on Redis."""

        if self._redis_checked:
            return True
        try:
            await check_redis_compatibility(self._redis)
        except RuntimeError:
            logger.exception(
                "Redis cannot run event Stream retention; skipping trimming"
            )
            return False
        except Exception:
            logger.warning(
                "Redis is unavailable; skipping event Stream trimming"
            )
            return False
        self._redis_checked = True
        return True

    async def _delete_published_outbox(self) -> int:
        cutoff = utc_now() - timedelta(
            seconds=self._settings.published_outbox_retention_seconds
        )
        async with self._session_factory() as session, session.begin():
            ids = list(
                await session.scalars(
                    select(OutboxEventORM.id)
                    .where(
                        OutboxEventORM.status == OutboxStatus.PUBLISHED,
                        OutboxEventORM.published_at.is_not(None),
                        OutboxEventORM.published_at < cutoff,
                    )
                    .order_by(OutboxEventORM.published_at, OutboxEventORM.id)
                    .limit(self._settings.event_retention_batch_size)
                )
            )
            if ids:
                await session.execute(
                    delete(OutboxEventORM).where(OutboxEventORM.id.in_(ids))
                )
        return len(ids)

    async def _delete_execution_events(self) -> int:
        cutoff = utc_now() - timedelta(
            seconds=self._settings.execution_event_retention_seconds
        )
        async with self._session_factory() as session, session.begin():
            ids = list(
                await session.scalars(
                    select(ExecutionEventORM.id)
                    .join(
                        ExecutionORM,
                        ExecutionORM.id == ExecutionEventORM.execution_id,
                    )
                    .where(
                        ExecutionORM.status.in_(_TERMINAL_EXECUTION_STATUSES),
                        ExecutionORM.finished_at.is_not(None),
                        ExecutionORM.finished_at < cutoff,
                        ~exists(
                            select(OutboxEventORM.id).where(
                                OutboxEventORM.execution_event_id
                                == ExecutionEventORM.id
                            )
                        ),
                    )
                    .order_by(
                        ExecutionEventORM.created_at,
                        ExecutionEventORM.id,
                    )
                    .limit(self._settings.event_retention_batch_size)
                )
            )
            if ids:
                await session.execute(
                    delete(ExecutionEventORM).where(
                        ExecutionEventORM.id.in_(ids)
                    )
                )
        return len(ids)

    async def _event_streams(self) -> list[str]:
        """The shared event Stream and every tenant Stream ever written."""

        async with self._session_factory() as session:
            tenant_ids = list(
                await session.scalars(
                    select(ExecutionORM.tenant_id)
                    .where(ExecutionORM.tenant_id.is_not(None))
                    .distinct()
                    .order_by(ExecutionORM.tenant_id)
                )
            )
        base = self._settings.redis_event_stream
        return [base] + [
            tenant_event_stream(base, tenant_id) for tenant_id in tenant_ids
        ]

    async def _trim_event_stream(self, stream: str, deadline: float) -> int:
        boundary = _cutoff_stream_id(
            self._settings.redis_event_retention_seconds
        )
        batch_size = min(
            self._settings.event_retention_batch_size, MAX_BATCH_SIZE
        )

        async def trim_batch() -> int:
            return await trim_before(
                self._redis, stream, boundary, count=batch_size
            )

        return await _drain(trim_batch, batch_size, deadline)


async def _drain(
    delete_batch: Callable[[], Awaitable[int]],
    batch_size: int,
    deadline: float,
) -> int:
    """Repeat full batches until a short one or the pass deadline."""
    deleted = 0
    while True:
        count = await delete_batch()
        deleted += count
        if count < batch_size or time.monotonic() >= deadline:
            return deleted


def _cutoff_stream_id(retention_seconds: int) -> str:
    cutoff = utc_now() - timedelta(seconds=retention_seconds)
    return f"{int(cutoff.timestamp() * 1000)}-0"
