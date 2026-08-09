"""Transactional outbox publisher for at-least-once Redis Stream delivery."""

import asyncio
import json
import logging
from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import OutboxStatus
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import OutboxEventORM
from executor_service.observability import OUTBOX_FAILURES, OUTBOX_PUBLISHED

logger = logging.getLogger(__name__)


class OutboxPublisher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        stream_name: str,
        poll_interval_seconds: float,
        batch_size: int,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._stream_name = stream_name
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="outbox-publisher")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                published = await self.publish_batch()
            except Exception:
                # Database may be unavailable or not migrated during a rolling start.
                logger.exception("Outbox polling failed")
                published = 0
            if published == 0:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._poll_interval_seconds
                    )
                except TimeoutError:
                    pass

    async def publish_batch(self) -> int:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            events = list(
                await session.scalars(
                    select(OutboxEventORM)
                    .where(
                        OutboxEventORM.status == OutboxStatus.PENDING,
                        OutboxEventORM.available_at <= now,
                    )
                    .order_by(OutboxEventORM.created_at)
                    .limit(self._batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            published = 0
            for event in events:
                try:
                    await self._redis.xadd(
                        self._stream_name,
                        {
                            "event_id": str(event.id),
                            "event_type": event.event_type,
                            "aggregate_type": event.aggregate_type,
                            "aggregate_id": str(event.aggregate_id),
                            "occurred_at": event.created_at.isoformat(),
                            "payload": json.dumps(event.payload, separators=(",", ":")),
                        },
                    )
                    event.status = OutboxStatus.PUBLISHED
                    event.published_at = utc_now()
                    event.last_error = None
                    published += 1
                    OUTBOX_PUBLISHED.inc()
                except Exception as exc:
                    event.attempt_count += 1
                    delay_seconds = min(2 ** min(event.attempt_count, 6), 60)
                    event.available_at = utc_now() + timedelta(seconds=delay_seconds)
                    event.last_error = f"{type(exc).__name__}: Redis publish failed"
                    OUTBOX_FAILURES.inc()
                    logger.warning("Outbox publish failed", extra={"event_id": str(event.id)})
            return published
