"""REST contract of the event Stream inspection API, without Redis."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio

from executor_service.application.event_streams import (
    ConsumerGroupView,
    ConsumerView,
    EventStreamEntryPage,
    EventStreamEntryView,
    EventStreamView,
    StreamPosition,
)
from executor_service.container import ApplicationContainer
from executor_service.domain.errors import EventStreamNotFoundError
from executor_service.interfaces.http.app import create_app
from executor_service.settings import Settings
from tests.auth_keys import TEST_AUTH_CLIENTS, TEST_HEADERS

AT = datetime(2026, 11, 19, 4, 1, 52, tzinfo=UTC)
POSITION = StreamPosition(id="1795400112000-0", at=AT)
STREAM = EventStreamView(
    tenant_id="dev",
    stream="executor.events.dev",
    exists=True,
    length=3,
    entries_added=10,
    first_entry=POSITION,
    last_entry=POSITION,
    groups=(
        ConsumerGroupView(
            name="agent",
            pending=1,
            lag=None,
            entries_read=9,
            last_delivered=POSITION,
            oldest_pending=None,
            consumers=(
                ConsumerView(name="c1", pending=1, idle_ms=5, inactive_ms=7),
            ),
        ),
    ),
)


class FakeInspector:
    def __init__(self) -> None:
        self.entries_call: dict[str, Any] = {}

    async def stream(self, tenant_id: str) -> EventStreamView:
        if tenant_id != "dev":
            raise EventStreamNotFoundError("No such Stream.")
        return STREAM

    async def entries(
        self, tenant_id: str, **filters: Any
    ) -> EventStreamEntryPage:
        self.entries_call = {"tenant_id": tenant_id, **filters}
        return EventStreamEntryPage(
            items=(
                EventStreamEntryView(
                    id=POSITION.id,
                    at=AT,
                    event_id="e1",
                    event_type="execution.completed",
                    schema_version="1.0",
                    execution_id="x1",
                    event_sequence=7,
                    occurred_at="2026-11-19T04:01:52+00:00",
                    tenant_id="dev",
                    payload={"status": "SUCCEEDED"},
                ),
            ),
            next_before=POSITION.id,
        )


@asynccontextmanager
async def _client(
    inspector: FakeInspector | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    container = ApplicationContainer(
        Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///:memory:",
            # Nothing listens here.
            redis_url="redis://localhost:6399/15",
            auth_clients=TEST_AUTH_CLIENTS,
        )
    )
    if inspector is not None:
        container.event_streams = inspector  # ty: ignore[invalid-assignment]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(container)),
        base_url="http://testserver/api/v1/event-streams",
        headers=TEST_HEADERS,
    ) as client:
        yield client
    await container.redis.aclose()


@pytest_asyncio.fixture
async def api() -> AsyncIterator[tuple[httpx.AsyncClient, FakeInspector]]:
    inspector = FakeInspector()
    async with _client(inspector) as client:
        yield client, inspector


async def test_unreachable_redis_is_a_503() -> None:
    async with _client() as client:
        response = await client.get("/dev")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "REDIS_UNAVAILABLE"


async def test_stream_detail_serializes_the_whole_view(api) -> None:
    client, _ = api

    response = await client.get("/dev")

    assert response.status_code == 200
    body = response.json()
    assert body["first_entry"] == {
        "id": "1795400112000-0",
        "at": "2026-11-19T04:01:52Z",
    }
    assert body["groups"] == [
        {
            "name": "agent",
            "pending": 1,
            "lag": None,
            "entries_read": 9,
            "last_delivered": body["first_entry"],
            "oldest_pending": None,
            "consumers": [
                {"name": "c1", "pending": 1, "idle_ms": 5, "inactive_ms": 7}
            ],
        }
    ]


async def test_missing_stream_is_a_domain_404(api) -> None:
    client, _ = api

    response = await client.get("/other-tenant")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EVENT_STREAM_NOT_FOUND"


async def test_entries_pass_filters_and_default_the_limit(api) -> None:
    client, inspector = api

    response = await client.get(
        "/dev/entries",
        params={"before": "1795400112000-3", "execution_id": "x1"},
    )

    assert response.status_code == 200
    assert inspector.entries_call == {
        "tenant_id": "dev",
        "limit": 50,
        "before": "1795400112000-3",
        "execution_id": "x1",
        "event_type": None,
    }
    body = response.json()
    assert body["next_before"] == "1795400112000-0"
    assert body["items"][0]["payload"] == {"status": "SUCCEEDED"}
    assert body["items"][0]["event_sequence"] == 7


@pytest.mark.parametrize(
    "path",
    [
        "/dev/entries?limit=201",
        "/dev/entries?before=not-an-id",
        "/dev/groups/agent/pending?limit=101",
        "/dev/groups/agent/pending?min_idle_ms=-1",
        "/Not_A_Tenant/entries",
    ],
)
async def test_out_of_range_requests_are_rejected(api, path: str) -> None:
    client, _ = api

    response = await client.get(path)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"
