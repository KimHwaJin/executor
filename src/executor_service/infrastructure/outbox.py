"""Transactional outbox publisher for at-least-once Redis Stream delivery."""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError
from redis.typing import EncodableT, FieldT
from sqlalchemy import exists, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from executor_service.domain.enums import OutboxStatus
from executor_service.domain.models import utc_now
from executor_service.domain.tenants import tenant_event_stream
from executor_service.infrastructure.db.models import (
    ExecutionORM,
    OutboxEventORM,
)
from executor_service.infrastructure.events import (
    validate_execution_event_payload,
)

logger = logging.getLogger(__name__)


class OutboxPublisher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        event_stream_name: str,
        poll_interval_seconds: float,
        batch_size: int,
        publish_timeout_seconds: float = 5,
        shutdown_timeout_seconds: float = 10,
        event_stream_maxlen: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._stream_name = event_stream_name
        self._event_stream_maxlen = event_stream_maxlen
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size
        self._publish_timeout_seconds = publish_timeout_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="outbox-publisher"
            )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._task),
                    timeout=self._shutdown_timeout_seconds,
                )
            except TimeoutError:
                logger.warning("Outbox shutdown deadline exceeded; cancelling")
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
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
                        self._stop_event.wait(),
                        timeout=self._poll_interval_seconds,
                    )
                except TimeoutError:
                    pass

    async def publish_batch(self) -> int:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            earlier = aliased(OutboxEventORM)
            events = list(
                await session.scalars(
                    select(OutboxEventORM)
                    .where(
                        OutboxEventORM.status == OutboxStatus.PENDING,
                        OutboxEventORM.available_at <= now,
                        ~exists(
                            select(earlier.id).where(
                                earlier.aggregate_type
                                == OutboxEventORM.aggregate_type,
                                earlier.aggregate_id
                                == OutboxEventORM.aggregate_id,
                                earlier.event_sequence
                                < OutboxEventORM.event_sequence,
                                earlier.status == OutboxStatus.PENDING,
                            )
                        ),
                    )
                    .order_by(OutboxEventORM.created_at)
                    .limit(self._batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            await self._append_contiguous_execution_events(
                session,
                events,
                now=now,
            )
            tenants = await self._execution_tenants(session, events)
            published = 0
            blocked_aggregates: set[tuple[str, UUID]] = set()
            for event in events:
                aggregate_key = (event.aggregate_type, event.aggregate_id)
                if aggregate_key in blocked_aggregates:
                    continue
                try:
                    if event.event_sequence is None:
                        raise ValueError(
                            "Execution event_sequence is required."
                        )
                    public_event = event.execution_event
                    if public_event is None:
                        raise ValueError(
                            "Durable Execution event is required."
                        )
                    payload = validate_execution_event_payload(
                        public_event.event_type,
                        public_event.payload,
                    )
                    tenant_id = tenants.get(public_event.execution_id)
                    fields = cast(
                        dict[FieldT, EncodableT],
                        {
                            "event_id": str(public_event.id),
                            "event_type": public_event.event_type,
                            "schema_version": public_event.schema_version,
                            "execution_id": str(public_event.execution_id),
                            "event_sequence": str(public_event.event_sequence),
                            "payload": json.dumps(
                                payload, separators=(",", ":")
                            ),
                            "occurred_at": public_event.created_at.isoformat(),
                        },
                    )
                    if tenant_id is not None:
                        fields["tenant_id"] = tenant_id
                    stream = tenant_event_stream(self._stream_name, tenant_id)
                    async with asyncio.timeout(self._publish_timeout_seconds):
                        if self._event_stream_maxlen is None:
                            await self._redis.xadd(stream, fields)
                        else:
                            # Redis 6.0 has no MINID; MAXLEN ~ is cheap.
                            await self._redis.xadd(
                                stream,
                                fields,
                                maxlen=self._event_stream_maxlen,
                                approximate=True,
                            )
                    event.status = OutboxStatus.PUBLISHED
                    event.published_at = utc_now()
                    event.last_error = None
                    published += 1
                except Exception as exc:
                    blocked_aggregates.add(aggregate_key)
                    event.attempt_count += 1
                    delay_seconds = min(2 ** min(event.attempt_count, 6), 60)
                    event.available_at = utc_now() + timedelta(
                        seconds=delay_seconds
                    )
                    event.last_error = (
                        f"{type(exc).__name__}: Redis publish failed"
                    )
                    logger.warning(
                        "Outbox publish failed",
                        extra={"event_id": str(event.id)},
                    )
                    if isinstance(
                        exc, (TimeoutError, ConnectionError, RedisError)
                    ):
                        # Release this batch's DB locks promptly instead of
                        # multiplying a network deadline by the batch size.
                        break
            await session.flush()
            return published

    async def _execution_tenants(
        self, session: AsyncSession, events: list[OutboxEventORM]
    ) -> dict[UUID, str | None]:
        execution_ids = {
            event.execution_event.execution_id for event in events
        }
        if not execution_ids:
            return {}
        rows = await session.execute(
            select(ExecutionORM.id, ExecutionORM.tenant_id).where(
                ExecutionORM.id.in_(execution_ids)
            )
        )
        return {execution_id: tenant_id for execution_id, tenant_id in rows}

    async def _append_contiguous_execution_events(
        self,
        session: AsyncSession,
        events: list[OutboxEventORM],
        *,
        now: datetime,
    ) -> None:
        remaining = self._batch_size - len(events)
        if remaining <= 0:
            return
        if not events:
            return
        aggregate_keys = {
            (event.aggregate_type, event.aggregate_id) for event in events
        }
        head_ids = {event.id for event in events}
        unavailable_predecessor = aliased(OutboxEventORM)
        followers = list(
            await session.scalars(
                select(OutboxEventORM)
                .where(
                    OutboxEventORM.status == OutboxStatus.PENDING,
                    OutboxEventORM.available_at <= now,
                    tuple_(
                        OutboxEventORM.aggregate_type,
                        OutboxEventORM.aggregate_id,
                    ).in_(aggregate_keys),
                    OutboxEventORM.id.not_in(head_ids),
                    ~exists(
                        select(unavailable_predecessor.id).where(
                            unavailable_predecessor.aggregate_type
                            == OutboxEventORM.aggregate_type,
                            unavailable_predecessor.aggregate_id
                            == OutboxEventORM.aggregate_id,
                            unavailable_predecessor.status
                            == OutboxStatus.PENDING,
                            unavailable_predecessor.event_sequence
                            < OutboxEventORM.event_sequence,
                            unavailable_predecessor.id.not_in(head_ids),
                            unavailable_predecessor.available_at > now,
                        )
                    ),
                )
                .order_by(
                    OutboxEventORM.aggregate_type,
                    OutboxEventORM.aggregate_id,
                    OutboxEventORM.event_sequence,
                )
                .limit(remaining)
                .with_for_update(skip_locked=True)
            )
        )
        events.extend(followers)
