"""Run unchanged against Redis 6.0.8 and 7.4; never flush a database."""

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.event_retention import (
    EventRetentionManager,
)
from executor_service.infrastructure.redis_streams import (
    check_redis_compatibility,
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


async def test_server_accepts_required_commands(redis: Redis) -> None:
    await check_redis_compatibility(redis)
    expected = os.getenv("EXECUTOR_EXPECT_REDIS_VERSION")
    if expected:
        assert (await redis.info("server"))["redis_version"] == expected


async def test_retention_keeps_cutoff_and_obeys_batch_limit(
    redis: Redis, stream: str
) -> None:
    for index in range(1, 8):
        await redis.xadd(stream, {"value": str(index)}, id=f"100-{index}")
    assert await trim_before(redis, stream, "100-6", count=2) == 2
    assert await trim_before(redis, stream, "100-6", count=10) == 3
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
    assert await trim_before(redis, stream, ids[-1], count=10) == 3
    assert [key for key, _ in await redis.xrange(stream)] == ids[-1:]


async def test_retention_missing_stream_is_a_no_op(
    redis: Redis, stream: str
) -> None:
    assert await trim_before(redis, stream, "200-0", count=10) == 0


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
        Settings(
            event_retention_batch_size=1500,
            redis_event_stream=stream,
            redis_event_retention_seconds=3600,
        ),
    )
    assert await manager._trim_event_stream() == 1500
    assert await redis.xlen(stream) == 700
    assert await manager._trim_event_stream() == 700
