"""Bound Redis transport and durable event-history retention."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import ExecutionStatus, OutboxStatus
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    EventRetentionLeaseORM,
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

RETENTION_LEASE_KEY = "events"
_TERMINAL_EXECUTION_STATUSES = (
    ExecutionStatus.SUCCEEDED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
)


@dataclass(frozen=True, slots=True)
class EventRetentionResult:
    published_outbox_deleted: int
    execution_events_deleted: int
    work_stream_trimmed: int
    event_stream_trimmed: int
    work_dlq_trimmed: int


class EventRetentionManager:
    """Run one bounded retention pass under a PostgreSQL-backed lease."""

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
        self._stopping = asyncio.Event()

    async def initialize(self) -> None:
        async with self._session_factory() as session, session.begin():
            row = await session.get(
                EventRetentionLeaseORM, RETENTION_LEASE_KEY
            )
            if row is None:
                now = utc_now()
                session.add(
                    EventRetentionLeaseORM(
                        singleton_key=RETENTION_LEASE_KEY,
                        created_at=now,
                        updated_at=now,
                    )
                )

    def start(self) -> None:
        if (
            not self._settings.event_retention_enabled
            or self._task is not None
        ):
            return
        self._stopping.clear()
        self._task = asyncio.create_task(
            self._run_loop(), name="event-retention"
        )

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _run_loop(self) -> None:
        interval = self._settings.event_retention_interval_seconds
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=interval)
                continue
            except TimeoutError:
                pass
            try:
                await self.run_once()
            except Exception:
                logger.exception("Event retention pass failed")

    async def run_once(self) -> EventRetentionResult | None:
        if not await self._acquire_lease():
            return None
        try:
            outbox_deleted, events_deleted = await self._cleanup_database()
            work_trimmed = await self._trim_work_stream()
            event_trimmed = await self._trim_by_age(
                self._settings.redis_event_stream,
                self._settings.redis_event_retention_seconds,
            )
            work_dlq_trimmed = await self._trim_by_age(
                self._settings.redis_work_dead_letter_stream,
                self._settings.redis_work_dlq_retention_seconds,
            )
        except Exception as exc:
            await self._release_lease(error=str(exc)[:1000])
            raise
        await self._release_lease(error=None)
        return EventRetentionResult(
            published_outbox_deleted=outbox_deleted,
            execution_events_deleted=events_deleted,
            work_stream_trimmed=work_trimmed,
            event_stream_trimmed=event_trimmed,
            work_dlq_trimmed=work_dlq_trimmed,
        )

    async def _acquire_lease(self) -> bool:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(EventRetentionLeaseORM)
                .where(
                    EventRetentionLeaseORM.singleton_key == RETENTION_LEASE_KEY
                )
                .with_for_update()
            )
            if row is None:
                row = EventRetentionLeaseORM(
                    singleton_key=RETENTION_LEASE_KEY,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                await session.flush()
            if (
                row.lease_owner is not None
                and row.lease_owner != self._owner
                and row.lease_expires_at is not None
                and _as_utc(row.lease_expires_at) > now
            ):
                return False
            row.lease_owner = self._owner
            row.lease_expires_at = now + timedelta(
                seconds=self._settings.event_retention_lease_seconds
            )
            row.last_started_at = now
            row.last_error = None
            row.updated_at = now
        return True

    async def _release_lease(self, *, error: str | None) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(EventRetentionLeaseORM)
                .where(
                    EventRetentionLeaseORM.singleton_key
                    == RETENTION_LEASE_KEY,
                    EventRetentionLeaseORM.lease_owner == self._owner,
                )
                .with_for_update()
            )
            if row is None:
                return
            row.lease_owner = None
            row.lease_expires_at = None
            row.last_completed_at = now
            row.last_error = error
            row.updated_at = now

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
                        OutboxEventORM.status == OutboxStatus.PUBLISHED,
                        OutboxEventORM.published_at.is_not(None),
                        OutboxEventORM.published_at < outbox_cutoff,
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

    async def _trim_work_stream(self) -> int:
        return await self._trim_stream(
            self._settings.redis_work_stream,
            self._settings.redis_work_retention_seconds,
            protect_groups=True,
        )

    async def _trim_by_age(self, stream: str, retention_seconds: int) -> int:
        return await self._trim_stream(
            stream, retention_seconds, protect_groups=False
        )

    async def _trim_stream(
        self, stream: str, retention_seconds: int, *, protect_groups: bool
    ) -> int:
        remaining = self._settings.event_retention_batch_size
        deleted = 0
        boundary = _cutoff_stream_id(retention_seconds)
        while remaining > 0:
            batch_size = min(remaining, MAX_BATCH_SIZE)
            count = await trim_before(
                self._redis,
                stream,
                boundary,
                protect_groups=protect_groups,
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


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
