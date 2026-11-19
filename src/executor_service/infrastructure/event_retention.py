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
from redis.exceptions import RedisError
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
        return EventRetentionResult(
            published_outbox_deleted=outbox_deleted,
            execution_events_deleted=events_deleted,
            event_stream_trimmed=await self._trim_event_streams(),
        )

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
        """Every tenant Stream ever written."""

        return [
            tenant_event_stream(self._settings.redis_event_stream, tenant_id)
            for tenant_id in await event_stream_tenant_ids(
                self._session_factory
            )
        ]

    async def _trim_event_streams(self) -> int:
        """Drop entries older than the cutoff; Redis being down skips it."""

        boundary = _cutoff_stream_id(
            self._settings.redis_event_retention_seconds
        )
        trimmed = 0
        try:
            for stream in await self._event_streams():
                # Exact MINID keeps the cutoff entry and frees whole old
                # nodes; ~ with LIMIT below a node's size trims nothing.
                trimmed += await self._redis.xtrim(
                    stream, minid=boundary, approximate=False
                )
        except (RedisError, OSError) as exc:
            # Down, or denied by ACL: PostgreSQL cleanup already ran.
            logger.warning(
                "Event Stream trimming skipped: %s", type(exc).__name__
            )
        return trimmed


async def event_stream_tenant_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[str]:
    async with session_factory() as session:
        return list(
            await session.scalars(
                select(ExecutionORM.tenant_id)
                .distinct()
                .order_by(ExecutionORM.tenant_id)
            )
        )


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
