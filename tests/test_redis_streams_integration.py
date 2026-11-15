"""Run against a real Redis 8 server; never flush a database."""

import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.event_retention import (
    EventRetentionManager,
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
