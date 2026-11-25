"""Read-only view of the per-tenant event Streams and their consumers."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.event_streams import (
    ConsumerGroupView,
    ConsumerView,
    EventStreamEntryPage,
    EventStreamEntryView,
    EventStreamOverview,
    EventStreamView,
    PendingEntryPage,
    PendingEntryView,
    RedisConnectionView,
    StreamPosition,
)
from executor_service.domain.errors import (
    EventStreamNotFoundError,
    RedisUnavailableError,
)
from executor_service.domain.tenants import tenant_event_stream
from executor_service.infrastructure.event_retention import (
    event_stream_tenant_ids,
)
from executor_service.infrastructure.redis_client import redis_address
from executor_service.settings import Settings

# Entries one filtered request may read, so its cost on Redis stays bounded.
FILTER_SCAN_LIMIT = 1000
_FILTER_PAGE = 200


class RedisEventStreamInspector:
    """Issues EXISTS, XINFO, XREVRANGE, XRANGE and XPENDING only."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._settings = settings

    async def overview(self) -> EventStreamOverview:
        tenant_ids = await event_stream_tenant_ids(self._session_factory)
        with _redis_errors():
            return EventStreamOverview(
                connection=RedisConnectionView(
                    mode=self._settings.redis_mode,
                    address=await redis_address(self._redis),
                ),
                retention_seconds=self._settings.redis_event_retention_seconds,
                max_length=self._settings.redis_event_stream_maxlen,
                streams=tuple(
                    [await self._view(tenant_id) for tenant_id in tenant_ids]
                ),
            )

    async def stream(self, tenant_id: str) -> EventStreamView:
        with _redis_errors():
            view = await self._view(tenant_id)
        if not view.exists:
            raise EventStreamNotFoundError(
                f"Event Stream {view.stream} does not exist."
            )
        return view

    async def entries(
        self,
        tenant_id: str,
        *,
        limit: int,
        before: str | None = None,
        execution_id: str | None = None,
        event_type: str | None = None,
    ) -> EventStreamEntryPage:
        if not 1 <= limit <= 200:
            raise ValueError("Entry page limit must be between 1 and 200.")
        stream = self._stream(tenant_id)
        filtered = execution_id is not None or event_type is not None
        items: list[EventStreamEntryView] = []
        cursor = before
        budget = FILTER_SCAN_LIMIT if filtered else limit
        with _redis_errors():
            if not await self._redis.exists(stream):
                raise EventStreamNotFoundError(
                    f"Event Stream {stream} does not exist."
                )
            while len(items) < limit and budget > 0:
                count = min(_FILTER_PAGE, budget) if filtered else limit
                page = await self._redis.xrevrange(
                    stream, max=f"({cursor}" if cursor else "+", count=count
                )
                budget -= len(page)
                for entry_id, fields in page:
                    cursor = entry_id
                    if execution_id in (None, fields.get("execution_id")) and (
                        event_type in (None, fields.get("event_type"))
                    ):
                        items.append(_entry(entry_id, fields))
                        if len(items) == limit:
                            break
                else:
                    if len(page) < count:
                        cursor = None
                        break
        return EventStreamEntryPage(items=tuple(items), next_before=cursor)

    async def pending(
        self,
        tenant_id: str,
        group: str,
        *,
        limit: int,
        consumer: str | None = None,
        min_idle_ms: int = 0,
    ) -> PendingEntryPage:
        stream = self._stream(tenant_id)
        with _redis_errors():
            try:
                summary = await self._redis.xpending(stream, group)
                rows = await self._redis.xpending_range(
                    stream,
                    group,
                    min="-",
                    max="+",
                    count=limit,
                    consumername=consumer,
                    idle=min_idle_ms or None,
                )
            except ResponseError as exc:
                if "NOGROUP" not in str(exc):
                    raise
                raise EventStreamNotFoundError(
                    f"Event Stream {stream} has no consumer group {group}."
                ) from None
            lookup = self._redis.pipeline(transaction=False)
            for row in rows:
                lookup.xrange(stream, row["message_id"], row["message_id"])
            stored = await lookup.execute() if rows else []
        items = []
        for row, found in zip(rows, stored, strict=True):
            fields = found[0][1] if found else {}
            items.append(
                PendingEntryView(
                    id=row["message_id"],
                    at=_at(row["message_id"]),
                    consumer=row["consumer"],
                    idle_ms=row["time_since_delivered"],
                    delivery_count=row["times_delivered"],
                    execution_id=fields.get("execution_id"),
                    event_type=fields.get("event_type"),
                    event_sequence=_sequence(fields),
                )
            )
        return PendingEntryPage(
            group=group, total_pending=summary["pending"], items=tuple(items)
        )

    def _stream(self, tenant_id: str) -> str:
        return tenant_event_stream(
            self._settings.redis_event_stream, tenant_id
        )

    async def _view(self, tenant_id: str) -> EventStreamView:
        stream = self._stream(tenant_id)
        if not await self._redis.exists(stream):
            return EventStreamView(
                tenant_id=tenant_id,
                stream=stream,
                exists=False,
                length=0,
                entries_added=0,
                first_entry=None,
                last_entry=None,
                groups=(),
            )
        info = await self._redis.xinfo_stream(stream)
        groups = await self._redis.xinfo_groups(stream)
        # One round trip for every group's consumers and pending range.
        detail = self._redis.pipeline(transaction=False)
        for group in groups:
            detail.xinfo_consumers(stream, group["name"])
            detail.xpending(stream, group["name"])
        details = await detail.execute() if groups else []
        return EventStreamView(
            tenant_id=tenant_id,
            stream=stream,
            exists=True,
            length=info["length"],
            entries_added=info["entries-added"],
            first_entry=_entry_position(info["first-entry"]),
            last_entry=_entry_position(info["last-entry"]),
            groups=tuple(
                ConsumerGroupView(
                    name=group["name"],
                    pending=group["pending"],
                    lag=group["lag"],
                    entries_read=group["entries-read"],
                    last_delivered=_position(group["last-delivered-id"]),
                    oldest_pending=_position(pending["min"]),
                    consumers=tuple(
                        ConsumerView(
                            name=consumer["name"],
                            pending=consumer["pending"],
                            idle_ms=consumer["idle"],
                            inactive_ms=consumer.get("inactive"),
                        )
                        for consumer in consumers
                    ),
                )
                for group, consumers, pending in zip(
                    groups, details[::2], details[1::2], strict=True
                )
            ),
        )


@contextmanager
def _redis_errors() -> Iterator[None]:
    try:
        yield
    except (RedisError, OSError, TimeoutError) as exc:
        # Down, failing over, or the command is denied by ACL.
        raise RedisUnavailableError(
            f"Redis request failed: {type(exc).__name__}."
        ) from exc


def _at(entry_id: str) -> datetime:
    milliseconds = int(entry_id.partition("-")[0])
    return datetime.fromtimestamp(milliseconds / 1000, UTC)


def _position(entry_id: str | None) -> StreamPosition | None:
    """None for no entry: an empty range, or a group that read nothing."""

    if not entry_id or entry_id == "0-0":
        return None
    return StreamPosition(id=entry_id, at=_at(entry_id))


def _sequence(fields: dict[str, str]) -> int | None:
    value = fields.get("event_sequence", "")
    return int(value) if value.isdecimal() else None


def _entry_position(entry: Any) -> StreamPosition | None:
    return _position(entry[0]) if entry else None


def _entry(entry_id: str, fields: dict[str, str]) -> EventStreamEntryView:
    payload: Any = fields.get("payload")
    try:
        payload = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        pass
    return EventStreamEntryView(
        id=entry_id,
        at=_at(entry_id),
        event_id=fields.get("event_id"),
        event_type=fields.get("event_type"),
        schema_version=fields.get("schema_version"),
        execution_id=fields.get("execution_id"),
        event_sequence=_sequence(fields),
        occurred_at=fields.get("occurred_at"),
        tenant_id=fields.get("tenant_id"),
        payload=payload,
    )
