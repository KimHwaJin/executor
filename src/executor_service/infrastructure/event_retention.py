"""Bound Redis transport and durable event-history retention."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import (
    ExecutionStatus,
    OutboxDestination,
    OutboxStatus,
)
from executor_service.domain.models import utc_now
from executor_service.domain.tenants import tenant_event_stream
from executor_service.infrastructure.cluster_passes import (
    claim_pass,
    run_cluster_pass_loop,
)
from executor_service.infrastructure.db.models import (
    ExecutionEventORM,
    ExecutionORM,
    OutboxEventORM,
)
from executor_service.infrastructure.redis_streams import (
    MAX_BATCH_SIZE,
    trim_before,
)
from executor_service.settings import Settings

logger = logging.getLogger(__name__)

RETENTION_PASS_KEY = "events"
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

    async def run_once(self) -> EventRetentionResult | None:
        if not await claim_pass(
            self._session_factory,
            RETENTION_PASS_KEY,
            self._owner,
            self._settings.event_retention_interval_seconds,
        ):
            return None
        return await self.run_pass()

    async def run_pass(self) -> EventRetentionResult:
        outbox_deleted, events_deleted = await self._cleanup_database()
        event_trimmed = 0
        for stream in await self._event_streams():
            event_trimmed += await self._trim_event_stream(stream)
        return EventRetentionResult(
            published_outbox_deleted=outbox_deleted,
            execution_events_deleted=events_deleted,
            event_stream_trimmed=event_trimmed,
        )

    async def _cleanup_database(self) -> tuple[int, int]:
        now = utc_now()
        outbox_cutoff = now - timedelta(
            seconds=self._settings.published_outbox_retention_seconds
        )
        event_cutoff = now - timedelta(
            seconds=self._settings.execution_event_retention_seconds
        )
        batch_size = self._settings.event_retention_batch_size
        async with self._session_factory() as session, session.begin():
            outbox_ids = list(
                await session.scalars(
                    select(OutboxEventORM.id)
                    .where(
                        or_(
                            and_(
                                OutboxEventORM.status
                                == OutboxStatus.PUBLISHED,
                                OutboxEventORM.published_at.is_not(None),
                                OutboxEventORM.published_at < outbox_cutoff,
                            ),
                            OutboxEventORM.destination
                            == OutboxDestination.WORK,
                        )
                    )
                    .order_by(OutboxEventORM.published_at, OutboxEventORM.id)
                    .limit(batch_size)
                )
            )
            if outbox_ids:
                await session.execute(
                    delete(OutboxEventORM).where(
                        OutboxEventORM.id.in_(outbox_ids)
                    )
                )

            event_ids = list(
                await session.scalars(
                    select(ExecutionEventORM.id)
                    .join(
                        ExecutionORM,
                        ExecutionORM.id == ExecutionEventORM.execution_id,
                    )
                    .where(
                        ExecutionORM.status.in_(_TERMINAL_EXECUTION_STATUSES),
                        ExecutionORM.finished_at.is_not(None),
                        ExecutionORM.finished_at < event_cutoff,
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
                    .limit(batch_size)
                )
            )
            if event_ids:
                await session.execute(
                    delete(ExecutionEventORM).where(
                        ExecutionEventORM.id.in_(event_ids)
                    )
                )
        return len(outbox_ids), len(event_ids)

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

    async def _trim_event_stream(self, stream: str) -> int:
        remaining = self._settings.event_retention_batch_size
        deleted = 0
        boundary = _cutoff_stream_id(
            self._settings.redis_event_retention_seconds
        )
        while remaining > 0:
            batch_size = min(remaining, MAX_BATCH_SIZE)
            count = await trim_before(
                self._redis,
                stream,
                boundary,
                count=batch_size,
            )
            deleted += count
            remaining -= batch_size
            if count < batch_size:
                break
        return deleted


def _cutoff_stream_id(retention_seconds: int) -> str:
    cutoff = utc_now() - timedelta(seconds=retention_seconds)
    return f"{int(cutoff.timestamp() * 1000)}-0"
