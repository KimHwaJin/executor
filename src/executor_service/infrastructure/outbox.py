"""Transactional outbox publisher for at-least-once Redis Stream delivery."""

import asyncio
import json
import logging
from datetime import timedelta

from opentelemetry.trace import SpanKind
from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import OutboxDestination, OutboxStatus
from executor_service.domain.models import utc_now
from executor_service.events import validate_execution_event_payload
from executor_service.infrastructure.db.models import OutboxEventORM
from executor_service.tracing import (
    TracingManager,
    capture_trace_carrier,
    extract_trace_context,
)
from executor_service.work_messages import validate_work_payload

logger = logging.getLogger(__name__)


class OutboxPublisher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        work_stream_name: str,
        event_stream_name: str,
        poll_interval_seconds: float,
        batch_size: int,
        tracing: TracingManager,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._stream_names = {
            OutboxDestination.WORK: work_stream_name,
            OutboxDestination.EVENTS: event_stream_name,
        }
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size
        self._tracing = tracing
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
                    if event.destination == OutboxDestination.WORK:
                        payload = validate_work_payload(event.event_type, event.payload)
                        id_field = "message_id"
                        type_field = "message_type"
                    else:
                        payload = validate_execution_event_payload(event.event_type, event.payload)
                        id_field = "event_id"
                        type_field = "event_type"
                    if payload != event.payload:
                        # A deploy may find a pre-v1 PENDING row whose otherwise valid payload is
                        # missing only version normalization. Upgrade it in the same transaction
                        # that publishes and marks the row PUBLISHED.
                        event.payload = payload
                    context = extract_trace_context(
                        {
                            "traceparent": event.traceparent or "",
                            "tracestate": event.tracestate or "",
                        }
                    )
                    with self._tracing.span(
                        "executor.outbox.publish",
                        context=context,
                        kind=SpanKind.PRODUCER,
                        attributes={
                            "executor.event.id": str(event.id),
                            "executor.event.type": event.event_type,
                            "executor.execution.id": str(event.aggregate_id),
                        },
                    ):
                        fields: dict[FieldT, EncodableT] = {
                            id_field: str(event.id),
                            type_field: event.event_type,
                            "schema_version": str(payload["schema_version"]),
                            "aggregate_type": event.aggregate_type,
                            "aggregate_id": str(event.aggregate_id),
                            "occurred_at": event.created_at.isoformat(),
                            "payload": json.dumps(payload, separators=(",", ":")),
                        }
                        carrier = capture_trace_carrier()
                        if carrier.traceparent:
                            fields["traceparent"] = carrier.traceparent
                        if carrier.tracestate:
                            fields["tracestate"] = carrier.tracestate
                        await self._redis.xadd(self._stream_names[event.destination], fields)
                    event.status = OutboxStatus.PUBLISHED
                    event.published_at = utc_now()
                    event.last_error = None
                    published += 1
                except Exception as exc:
                    event.attempt_count += 1
                    delay_seconds = min(2 ** min(event.attempt_count, 6), 60)
                    event.available_at = utc_now() + timedelta(seconds=delay_seconds)
                    event.last_error = f"{type(exc).__name__}: Redis publish failed"
                    logger.warning("Outbox publish failed", extra={"event_id": str(event.id)})
            await session.flush()
            return published
