from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ExecutorEvent(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    event_id: UUID
    execution_id: UUID
    event_type: str = Field(min_length=1)
    event_sequence: int = Field(ge=1)
    schema_version: Literal["1.0"]
    occurred_at: str
    payload: dict[str, Any]

    def identity_document(self) -> dict[str, Any]:
        """Compare immutable envelope fields, not REST delivery metadata.

        Keep the public event and stored JSON unchanged. Normalizing both
        operands also supports Inbox rows written by older Workers.
        """
        data = self.model_dump(
            mode="json", include=set(ExecutorEvent.model_fields)
        )
        occurred_at = datetime.fromisoformat(self.occurred_at)
        if occurred_at.tzinfo is None:
            raise ValueError("Executor occurred_at requires a timezone")
        data["occurred_at"] = occurred_at.astimezone(UTC).isoformat(
            timespec="microseconds"
        )
        # JSONB can expand 1e24 to an integer. Decimal keeps the JSON numeric
        # value stable; a tuple tag keeps numbers distinct from booleans
        # and from user-provided JSON arrays. Never expose this internal
        # comparison document as an event or store it in the database.
        return json.loads(
            json.dumps(data, allow_nan=False),
            parse_int=_number_identity,
            parse_float=_number_identity,
        )

    @classmethod
    def from_redis(cls, fields: dict[str, str]) -> ExecutorEvent:
        return cls.model_validate(
            {**fields, "payload": json.loads(fields["payload"])}
        )


def _number_identity(value: str) -> tuple[Decimal]:
    return (Decimal(value),)


@dataclass(frozen=True)
class EventContext:
    namespace: str
    tenant: str
    session_id: str
    # None for Executions the Worker claimed (Route.claim).
    task_id: str | None
    execution_id: UUID
    command_id: UUID
    event: ExecutorEvent

    @property
    def graph_config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.session_id}}


EventHandler = Callable[[EventContext], Awaitable[None]]

# Executor's tenant_id format; it is also the event Stream name suffix.
TENANT_ID = re.compile(r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$")


@dataclass(frozen=True)
class Route:
    """Hand these event types of one tenant's Executions to a handler.

    Without ``claim`` the Route serves Executions the Agent registered
    (``Store.register``); their events wait until it does. With ``claim`` it
    serves Executions this service did not submit: the first event of an
    unregistered Execution for which ``claim`` is true makes the Worker bind
    that Execution from that event on. Both kinds can share a tenant and
    event type.
    """

    tenant: str
    event_types: Iterable[str]
    handler: EventHandler
    claim: Callable[[ExecutorEvent], bool] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_types", frozenset(self.event_types))
        if not TENANT_ID.fullmatch(self.tenant):
            raise ValueError(f"Invalid tenant: {self.tenant!r}")
        if not self.event_types or not all(
            isinstance(item, str) and item.strip() for item in self.event_types
        ):
            raise ValueError("A Route needs event types")
        if not callable(self.handler):
            raise TypeError("A Route handler must be callable")


class RouteTable:
    """One handler per (tenant, event type, registered or claimed)."""

    def __init__(self, routes: Iterable[Route]) -> None:
        self._handlers: dict[tuple[str, str, bool], EventHandler] = {}
        self._claims: dict[str, list[Callable[[ExecutorEvent], bool]]] = {}
        tenants: set[str] = set()
        for route in routes:
            tenants.add(route.tenant)
            claimed = route.claim is not None
            if claimed:
                self._claims.setdefault(route.tenant, []).append(route.claim)
            for event_type in route.event_types:
                key = (route.tenant, event_type, claimed)
                if key in self._handlers:
                    raise ValueError(f"Duplicate Route for {key}")
                self._handlers[key] = route.handler
        if not self._handlers:
            raise ValueError("At least one Route is required")
        self.tenants = sorted(tenants)

    def claims(self, tenant: str, event: ExecutorEvent) -> bool:
        """Whether the Worker should bind this event's Execution itself."""
        return any(claim(event) for claim in self._claims.get(tenant, ()))

    def handler(
        self, tenant: str, event_type: str, claimed: bool
    ) -> EventHandler | None:
        return self._handlers.get((tenant, event_type, claimed))


class DeferEvent(Exception):
    """Not ready yet: retain pending without spending a failure attempt."""


class RejectEvent(Exception):
    """Permanent business failure: persist FAILED and move to DLQ."""


class IgnoreEvent(Exception):
    """Explicitly record an obsolete event without resuming the graph."""
