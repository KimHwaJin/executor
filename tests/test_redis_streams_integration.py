"""Run against a real Redis 8 server; never flush a database."""

import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.domain.errors import EventStreamNotFoundError
from executor_service.infrastructure import event_stream_inspector
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.event_retention import (
    EventRetentionManager,
)
from executor_service.infrastructure.event_stream_inspector import (
    RedisEventStreamInspector,
)
from executor_service.settings import Settings

pytestmark = pytest.mark.redis


@pytest_asyncio.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(
        os.getenv("EXECUTOR_REDIS_TEST_URL", "redis://127.0.0.1:6379/15"),
        decode_responses=True,
    )
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        if os.getenv("EXECUTOR_REQUIRE_REDIS_TESTS") == "1":
            raise
        pytest.skip()
    try:
        yield client
    finally:
        await client.aclose()


@pytest_asyncio.fixture
async def stream(redis: Redis) -> AsyncIterator[str]:
    name = f"test:compat:{uuid4().hex}"
    try:
        yield name
    finally:
        await redis.delete(name)


async def test_server_is_the_expected_version(redis: Redis) -> None:
    expected = os.getenv("EXECUTOR_EXPECT_REDIS_VERSION")
    if expected:
        assert (await redis.info("server"))["redis_version"] == expected


async def test_retention_trims_only_entries_older_than_the_cutoff(
    redis: Redis,
    stream: str,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with redis.pipeline(transaction=False) as pipe:
        for index in range(1, 2201):
            pipe.xadd(stream, {"x": "1"}, id=f"100-{index}")
        await pipe.execute()
    fresh = [await redis.xadd(stream, {"x": "1"}) for _ in range(2)]
    missing = f"{stream}:missing"
    manager = EventRetentionManager(
        create_session_factory(engine),
        redis,
        Settings(redis_event_retention_seconds=3600),
    )
    monkeypatch.setattr(
        manager, "_event_streams", AsyncMock(return_value=[stream, missing])
    )

    assert await manager._trim_event_streams() == 2200
    assert [key for key, _ in await redis.xrange(stream)] == fresh
    assert not await redis.exists(missing)


@pytest_asyncio.fixture
async def inspector(
    redis: Redis, engine: AsyncEngine
) -> AsyncIterator[RedisEventStreamInspector]:
    prefix = f"test:inspect:{uuid4().hex}"
    try:
        yield RedisEventStreamInspector(
            create_session_factory(engine),
            redis,
            Settings(_env_file=None, redis_event_stream=prefix),
        )
    finally:
        await redis.delete(f"{prefix}.dev")


async def _publish(
    redis: Redis, inspector: RedisEventStreamInspector, count: int
) -> list[str]:
    stream = inspector._stream("dev")
    async with redis.pipeline(transaction=False) as pipe:
        for index in range(1, count + 1):
            pipe.xadd(
                stream,
                {
                    "execution_id": "target" if index % 400 == 0 else "other",
                    "event_type": "execution.started",
                    "event_sequence": str(index),
                    "payload": '{"status":"RUNNING"}',
                },
            )
        return await pipe.execute()


async def test_inspector_reports_groups_consumers_and_pending(
    redis: Redis, inspector: RedisEventStreamInspector
) -> None:
    ids = await _publish(redis, inspector, 3)
    stream = inspector._stream("dev")
    await redis.xgroup_create(stream, "agent", id="0")
    await redis.xgroup_create(stream, "idle", id="$")
    await redis.xreadgroup("agent", "c1", {stream: ">"}, count=2)

    view = await inspector.stream("dev")

    assert (view.length, view.entries_added) == (3, 3)
    assert view.first_entry is not None and view.first_entry.id == ids[0]
    assert view.last_entry is not None and view.last_entry.id == ids[2]
    agent, idle = view.groups
    assert (agent.name, agent.pending, agent.lag) == ("agent", 2, 1)
    assert agent.last_delivered is not None
    assert agent.last_delivered.id == ids[1]
    assert agent.oldest_pending is not None
    assert agent.oldest_pending.id == ids[0]
    assert [(c.name, c.pending) for c in agent.consumers] == [("c1", 2)]
    assert (idle.pending, idle.lag, idle.oldest_pending) == (0, 0, None)
    assert idle.consumers == ()

    # Trimming the first entry leaves it pending without its fields.
    await redis.xdel(stream, ids[0])
    page = await inspector.pending("dev", "agent", limit=10)

    assert page.total_pending == 2
    assert [(item.id, item.event_sequence) for item in page.items] == [
        (ids[0], None),
        (ids[1], 2),
    ]
    assert page.items[1].consumer == "c1"
    assert page.items[1].delivery_count == 1
    filtered = await inspector.pending(
        "dev", "agent", limit=10, consumer="nobody", min_idle_ms=60_000
    )
    assert filtered.items == ()


async def test_overview_lists_every_tenant_even_without_a_stream(
    redis: Redis,
    inspector: RedisEventStreamInspector,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _publish(redis, inspector, 1)
    monkeypatch.setattr(
        event_stream_inspector,
        "event_stream_tenant_ids",
        AsyncMock(return_value=["dev", "never-published"]),
    )

    overview = await inspector.overview()

    assert overview.connection.mode == "standalone"
    assert overview.connection.address.rpartition(":")[2].isdecimal()
    assert overview.retention_seconds == 604800
    assert [(s.tenant_id, s.exists, s.length) for s in overview.streams] == [
        ("dev", True, 1),
        ("never-published", False, 0),
    ]


async def test_inspector_pages_entries_and_bounds_a_filtered_scan(
    redis: Redis, inspector: RedisEventStreamInspector
) -> None:
    ids = await _publish(redis, inspector, 1300)

    newest = await inspector.entries("dev", limit=2)
    older = await inspector.entries("dev", limit=2, before=newest.next_before)

    assert [item.id for item in newest.items] == ids[:-3:-1]
    assert [item.id for item in older.items] == ids[-3:-5:-1]
    assert newest.items[0].payload == {"status": "RUNNING"}
    assert newest.items[0].event_sequence == 1300

    # Matches sit at 1200, 800 and 400; one request reads 1000 entries.
    first = await inspector.entries("dev", limit=5, execution_id="target")
    rest = await inspector.entries(
        "dev", limit=5, execution_id="target", before=first.next_before
    )

    assert [item.event_sequence for item in first.items] == [1200, 800, 400]
    assert first.next_before == ids[300]
    assert (rest.items, rest.next_before) == ((), None)
    one = await inspector.entries("dev", limit=1, execution_id="target")
    assert one.next_before == ids[1199]


async def test_inspector_tells_a_missing_stream_or_group_from_an_outage(
    redis: Redis, inspector: RedisEventStreamInspector
) -> None:
    with pytest.raises(EventStreamNotFoundError):
        await inspector.stream("dev")
    with pytest.raises(EventStreamNotFoundError):
        await inspector.entries("dev", limit=1)
    with pytest.raises(EventStreamNotFoundError):
        await inspector.pending("dev", "agent", limit=1)

    ids = await _publish(redis, inspector, 3)

    with pytest.raises(EventStreamNotFoundError, match="consumer group"):
        await inspector.pending("dev", "agent", limit=1)
    assert (await inspector.entries("dev", limit=5)).next_before is None
    # A full answer from a short page still leaves older entries to read.
    newest = await inspector.entries(
        "dev", limit=1, event_type="execution.started"
    )
    assert newest.next_before == ids[2]
