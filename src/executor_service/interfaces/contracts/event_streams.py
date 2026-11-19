"""Transport contracts for the Redis event Stream inspection API."""

from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field

from executor_service.interfaces.contracts.common import ContractModel


class _ViewModel(ContractModel):
    """Built straight from the application view of the same shape."""

    model_config = ConfigDict(from_attributes=True)


class StreamPositionResponse(_ViewModel):
    id: str = Field(description="Redis Stream entry ID.")
    at: datetime = Field(description="When Redis assigned the ID.")


class ConsumerResponse(_ViewModel):
    name: str
    pending: int = Field(description="Entries delivered but not acknowledged.")
    idle_ms: int = Field(description="Since its last attempt to read.")
    inactive_ms: int | None = Field(
        description="Since its last successful read or claim."
    )


class ConsumerGroupResponse(_ViewModel):
    name: str
    pending: int = Field(description="Entries delivered but not acknowledged.")
    lag: int | None = Field(
        description=(
            "Entries not yet delivered to the group; null when Redis cannot "
            "tell, such as after entries were deleted."
        )
    )
    entries_read: int | None
    last_delivered: StreamPositionResponse | None = Field(
        description="Null when the group has read nothing."
    )
    oldest_pending: StreamPositionResponse | None
    consumers: list[ConsumerResponse]


class EventStreamResponse(_ViewModel):
    tenant_id: str
    stream: str
    exists: bool = Field(
        description="False when Redis holds no Stream for the tenant."
    )
    length: int
    entries_added: int = Field(
        description="Entries ever written, including trimmed ones."
    )
    first_entry: StreamPositionResponse | None
    last_entry: StreamPositionResponse | None
    groups: list[ConsumerGroupResponse]


class RedisConnectionResponse(_ViewModel):
    mode: Literal["standalone", "sentinel"]
    address: str = Field(
        description=(
            "host:port Executor talks to; under Sentinel, the current master."
        )
    )


class EventStreamOverviewResponse(_ViewModel):
    connection: RedisConnectionResponse
    retention_seconds: int = Field(
        description="Entries older than this are trimmed."
    )
    max_length: int | None = Field(
        description="Approximate per-Stream cap; null when unset."
    )
    streams: list[EventStreamResponse]


class EventStreamEntryResponse(_ViewModel):
    """One entry as stored; fields a broken entry lacks are null."""

    id: str
    at: datetime
    event_id: str | None
    event_type: str | None
    schema_version: str | None
    execution_id: str | None
    event_sequence: int | None
    occurred_at: str | None
    tenant_id: str | None
    payload: Any = Field(
        description="Parsed JSON, or the stored text when it is not JSON."
    )


class EventStreamEntryPageResponse(_ViewModel):
    items: list[EventStreamEntryResponse] = Field(description="Newest first.")
    next_before: str | None = Field(
        description=(
            "Pass as `before` for older entries; null at the Stream's start."
        )
    )


class PendingEntryResponse(_ViewModel):
    id: str
    at: datetime
    consumer: str
    idle_ms: int = Field(description="Since the last delivery.")
    delivery_count: int
    execution_id: str | None = Field(
        description="Null, like the next two, once the entry was trimmed."
    )
    event_type: str | None
    event_sequence: int | None


class PendingEntryPageResponse(_ViewModel):
    group: str
    total_pending: int = Field(description="Before any filter.")
    items: list[PendingEntryResponse] = Field(description="Oldest first.")
