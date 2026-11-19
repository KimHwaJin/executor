"""Application read port for the Redis event Streams as Redis holds them."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class StreamPosition:
    """A Stream entry ID and the time Redis assigned it."""

    id: str
    at: datetime


@dataclass(frozen=True, slots=True)
class ConsumerView:
    name: str
    pending: int
    idle_ms: int
    inactive_ms: int | None


@dataclass(frozen=True, slots=True)
class ConsumerGroupView:
    name: str
    pending: int
    lag: int | None
    entries_read: int | None
    last_delivered: StreamPosition | None
    oldest_pending: StreamPosition | None
    consumers: tuple[ConsumerView, ...]


@dataclass(frozen=True, slots=True)
class EventStreamView:
    tenant_id: str
    stream: str
    exists: bool
    length: int
    entries_added: int
    first_entry: StreamPosition | None
    last_entry: StreamPosition | None
    groups: tuple[ConsumerGroupView, ...]


@dataclass(frozen=True, slots=True)
class RedisConnectionView:
    mode: str
    # host:port Executor talks to; under Sentinel, the current master.
    address: str


@dataclass(frozen=True, slots=True)
class EventStreamOverview:
    connection: RedisConnectionView
    retention_seconds: int
    max_length: int | None
    streams: tuple[EventStreamView, ...]


@dataclass(frozen=True, slots=True)
class EventStreamEntryView:
    """One entry as stored; fields a broken entry lacks are None."""

    id: str
    at: datetime
    event_id: str | None
    event_type: str | None
    schema_version: str | None
    execution_id: str | None
    event_sequence: int | None
    occurred_at: str | None
    tenant_id: str | None
    # Parsed JSON, or the stored text when it is not JSON.
    payload: Any


@dataclass(frozen=True, slots=True)
class EventStreamEntryPage:
    items: tuple[EventStreamEntryView, ...]
    # Pass as ``before`` for older entries; None at the Stream's start.
    next_before: str | None


@dataclass(frozen=True, slots=True)
class PendingEntryView:
    id: str
    at: datetime
    consumer: str
    idle_ms: int
    delivery_count: int
    # None once retention trimmed the entry.
    execution_id: str | None
    event_type: str | None
    event_sequence: int | None


@dataclass(frozen=True, slots=True)
class PendingEntryPage:
    group: str
    total_pending: int
    items: tuple[PendingEntryView, ...]


class EventStreamInspector(Protocol):
    """Reads only; never acknowledges, claims, trims or creates anything."""

    async def overview(self) -> EventStreamOverview: ...

    async def stream(self, tenant_id: str) -> EventStreamView: ...

    async def entries(
        self,
        tenant_id: str,
        *,
        limit: int,
        before: str | None = None,
        execution_id: str | None = None,
        event_type: str | None = None,
    ) -> EventStreamEntryPage: ...

    async def pending(
        self,
        tenant_id: str,
        group: str,
        *,
        limit: int,
        consumer: str | None = None,
        min_idle_ms: int = 0,
    ) -> PendingEntryPage: ...
