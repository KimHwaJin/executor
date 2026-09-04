"""Run unchanged against Redis 6.0.8 and 7.4; never flush a database."""

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.event_retention import (
    EventRetentionManager,
)
from executor_service.infrastructure.redis_streams import (
    check_redis_compatibility,
    claim_pending,
    trim_before,
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


async def _pending(redis: Redis, stream: str, count: int) -> list[str]:
    ids = [
        await redis.xadd(stream, {"text": f"결과-{i}"}) for i in range(count)
    ]
    await redis.xgroup_create(stream, "workers", id="0")
    await redis.xreadgroup("workers", "old", {stream: ">"}, count=count)
    # Deterministic stale delivery, not a wall-clock sleep.
    await redis.xclaim(stream, "workers", "old", 0, ids, idle=60000)
    return ids


async def test_server_accepts_required_commands(redis: Redis) -> None:
    await check_redis_compatibility(redis)
    expected = os.getenv("EXECUTOR_EXPECT_REDIS_VERSION")
    if expected:
        assert (await redis.info("server"))["redis_version"] == expected


async def test_pending_cursor_reaches_all_pages(
    redis: Redis, stream: str
) -> None:
    ids = await _pending(redis, stream, 7)
    cursor = "0-0"
    found = []
    for _ in range(5):
        batch = await claim_pending(
            redis,
            stream,
            "workers",
            "new",
            min_idle_ms=30000,
            start_id=cursor,
            count=2,
        )
        cursor = batch.next_cursor
        found.extend(key for key, _ in batch.messages)
        if batch.messages:
            await redis.xack(
                stream, "workers", *(key for key, _ in batch.messages)
            )
        if cursor == "0-0":
            break
    assert found == ids
    assert (await redis.xpending(stream, "workers"))["pending"] == 0


async def test_nonidle_page_does_not_starve_later_pending(
    redis: Redis, stream: str
) -> None:
    ids = await _pending(redis, stream, 4)
    await redis.execute_command(
        "XCLAIM", stream, "workers", "active", 0, *ids[:2], "IDLE", 0
    )
    first = await claim_pending(
        redis, stream, "workers", "new", min_idle_ms=30000, count=2
    )
    assert first.messages == []
    second = await claim_pending(
        redis,
        stream,
        "workers",
        "new",
        min_idle_ms=30000,
        start_id=first.next_cursor,
        count=2,
    )
    assert [key for key, _ in second.messages] == ids[2:]


async def test_two_consumers_cannot_simultaneously_claim_same_stale_page(
    redis: Redis, stream: str
) -> None:
    ids = await _pending(redis, stream, 10)
    batches = await asyncio.gather(
        *(
            claim_pending(
                redis, stream, "workers", consumer, min_idle_ms=30000
            )
            for consumer in ("a", "b")
        )
    )
    claimed = [key for batch in batches for key, _ in batch.messages]
    assert sorted(claimed) == sorted(ids)
    assert len(claimed) == len(set(claimed))


async def test_missing_payload_is_cleaned_and_logged(
    redis: Redis, stream: str, caplog: pytest.LogCaptureFixture
) -> None:
    ids = await _pending(redis, stream, 3)
    await redis.xdel(stream, ids[1])
    batch = await claim_pending(
        redis, stream, "workers", "new", min_idle_ms=30000
    )
    assert batch.deleted_ids == [ids[1]]
    assert [key for key, _ in batch.messages] == [ids[0], ids[2]]
    assert (await redis.xpending(stream, "workers"))["pending"] == 2
    assert "payload is missing" in caplog.text


async def test_acknowledged_between_passes_is_not_reclaimed(
    redis: Redis, stream: str
) -> None:
    ids = await _pending(redis, stream, 2)
    await redis.xack(stream, "workers", ids[0])
    batch = await claim_pending(
        redis, stream, "workers", "new", min_idle_ms=30000
    )
    assert [key for key, _ in batch.messages] == ids[1:]


async def test_claim_preserves_text_for_byte_clients(
    redis: Redis, stream: str
) -> None:
    await _pending(redis, stream, 1)
    client = Redis.from_url(
        os.getenv("EXECUTOR_REDIS_TEST_URL", "redis://127.0.0.1:6379/15")
    )
    try:
        batch = await claim_pending(
            client, stream, "workers", "new", min_idle_ms=30000
        )
        assert batch.messages[0][1] == {"text": "결과-0"}
    finally:
        await client.aclose()


async def test_missing_group_error_is_not_swallowed(
    redis: Redis, stream: str
) -> None:
    with pytest.raises(ResponseError, match="NOGROUP"):
        await claim_pending(redis, stream, "absent", "new", min_idle_ms=1)


async def test_retention_keeps_cutoff_and_obeys_batch_limit(
    redis: Redis, stream: str
) -> None:
    for index in range(1, 8):
        await redis.xadd(stream, {"value": str(index)}, id=f"100-{index}")
    assert (
        await trim_before(
            redis, stream, "100-6", protect_groups=False, count=2
        )
        == 2
    )
    assert (
        await trim_before(
            redis, stream, "100-6", protect_groups=False, count=10
        )
        == 3
    )
    assert [key for key, _ in await redis.xrange(stream)] == ["100-6", "100-7"]


async def test_retention_compares_uint64_ids_without_rounding(
    redis: Redis, stream: str
) -> None:
    ids = [
        "9007199254740992-0",
        "9007199254740993-0",
        "9007199254740993-9007199254740992",
        "9007199254740993-9007199254740993",
    ]
    for key in ids:
        await redis.xadd(stream, {"x": "1"}, id=key)
    assert (
        await trim_before(
            redis, stream, ids[-1], protect_groups=False, count=10
        )
        == 3
    )
    assert [key for key, _ in await redis.xrange(stream)] == ids[-1:]


@pytest.mark.parametrize("unread_group", [False, True])
async def test_work_retention_protects_all_groups_pending_and_unread(
    redis: Redis, stream: str, unread_group: bool
) -> None:
    ids = await _pending(redis, stream, 8)
    await redis.xack(stream, "workers", *ids[:6])
    await redis.xgroup_create(stream, "slow", id="0")
    if not unread_group:
        await redis.xreadgroup("slow", "slow-a", {stream: ">"}, count=4)
        await redis.xack(stream, "slow", *ids[:2])
    deleted = await trim_before(
        redis, stream, "18446744073709551615-0", protect_groups=True, count=100
    )
    assert deleted == (0 if unread_group else 2)
    assert [key for key, _ in await redis.xrange(stream)] == (
        ids if unread_group else ids[2:]
    )


async def test_work_retention_with_acked_delivery_does_not_delete_unread(
    redis: Redis, stream: str
) -> None:
    for index in range(1, 6):
        await redis.xadd(stream, {"x": "1"}, id=f"100-{index}")
    await redis.xgroup_create(stream, "workers", id="0")
    await redis.xreadgroup("workers", "a", {stream: ">"}, count=3)
    await redis.xack(stream, "workers", "100-1", "100-2", "100-3")
    assert (
        await trim_before(
            redis, stream, "200-0", protect_groups=True, count=10
        )
        == 2
    )
    assert [key for key, _ in await redis.xrange(stream)] == [
        "100-3",
        "100-4",
        "100-5",
    ]


async def test_retention_missing_stream_and_work_without_group(
    redis: Redis, stream: str
) -> None:
    assert (
        await trim_before(
            redis, stream, "200-0", protect_groups=False, count=10
        )
        == 0
    )
    await redis.xadd(stream, {"x": "1"}, id="100-0")
    assert (
        await trim_before(
            redis, stream, "200-0", protect_groups=True, count=10
        )
        == 0
    )
    assert await redis.xlen(stream) == 1


async def test_retention_racing_claim_keeps_all_pending_payloads(
    redis: Redis, stream: str
) -> None:
    ids = await _pending(redis, stream, 20)
    await redis.xack(stream, "workers", *ids[:5])
    batch, deleted = await asyncio.gather(
        claim_pending(redis, stream, "workers", "new", min_idle_ms=30000),
        trim_before(
            redis,
            stream,
            "18446744073709551615-0",
            protect_groups=True,
            count=100,
        ),
    )
    assert deleted == 5
    assert [key for key, _ in batch.messages] == ids[5:]
    assert [key for key, _ in await redis.xrange(stream)] == ids[5:]


async def test_retention_manager_caps_total_work_per_pass(
    redis: Redis, stream: str, engine: AsyncEngine
) -> None:
    async with redis.pipeline(transaction=False) as pipe:
        for index in range(1, 2201):
            pipe.xadd(stream, {"x": "1"}, id=f"100-{index}")
        await pipe.execute()
    manager = EventRetentionManager(
        create_session_factory(engine),
        redis,
        Settings(event_retention_batch_size=1500),
    )
    assert await manager._trim_by_age(stream, 3600) == 1500
    assert await redis.xlen(stream) == 700
    assert await manager._trim_by_age(stream, 3600) == 700


async def test_maximum_pending_batch_and_cursor_wrap(
    redis: Redis, stream: str
) -> None:
    ids = await _pending(redis, stream, 1000)
    batch = await claim_pending(
        redis, stream, "workers", "new", min_idle_ms=30000, count=1000
    )
    assert [key for key, _ in batch.messages] == ids
    last = await claim_pending(
        redis,
        stream,
        "workers",
        "new",
        min_idle_ms=30000,
        count=1000,
        start_id=batch.next_cursor,
    )
    assert last.next_cursor == "0-0"
    assert last.messages == []
