"""Validation and startup contracts, without external Redis."""

import asyncio
import time
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from executor_service.container import ApplicationContainer
from executor_service.infrastructure.redis_streams import (
    check_redis_compatibility,
    trim_before,
)
from executor_service.settings import Settings


@pytest.mark.parametrize(
    "value",
    ["-", "(1-0", "1", "-1-0", "1--1", "1-18446744073709551616", "\uff11-0"],
)
async def test_invalid_stream_id_is_rejected(value: str) -> None:
    redis = AsyncMock()
    with pytest.raises(ValueError):
        await trim_before(cast(Redis, redis), "s", value, count=1)
    redis.execute_command.assert_not_awaited()


@pytest.mark.parametrize("version", ["6.0.7", "5.0.14", "6.0", "unknown"])
async def test_startup_rejects_unsupported_version(version: str) -> None:
    redis = AsyncMock()
    redis.info.return_value = {"redis_version": version}
    with pytest.raises(RuntimeError, match="Redis"):
        await check_redis_compatibility(cast(Redis, redis))
    redis.execute_command.assert_not_awaited()


async def test_acl_error_is_not_misreported_as_supported() -> None:
    redis = AsyncMock()
    redis.info.return_value = {"redis_version": "6.0.8"}
    redis.execute_command.side_effect = ResponseError("NOPERM")
    with pytest.raises(ResponseError, match="NOPERM"):
        await check_redis_compatibility(cast(Redis, redis))


@pytest.mark.parametrize("failure", [None, TypeError("nil command")])
async def test_missing_commands_have_clear_startup_error(
    failure: Exception | None,
) -> None:
    redis = AsyncMock()
    redis.info.return_value = {"redis_version": "6.0.8"}
    redis.execute_command.return_value = {}
    redis.execute_command.side_effect = failure
    with pytest.raises(RuntimeError, match="lacks required"):
        await check_redis_compatibility(cast(Redis, redis))


@pytest.mark.parametrize("count", [0, 1001])
async def test_batch_limits_apply_before_any_redis_call(count: int) -> None:
    redis = AsyncMock()
    with pytest.raises(ValueError):
        await trim_before(cast(Redis, redis), "s", "100-0", count=count)
    redis.execute_command.assert_not_awaited()


async def test_startup_does_not_need_redis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = ApplicationContainer(
        Settings(
            _env_file=None,
            shared_storage_root=tmp_path,
            db_auto_migrate=False,
            runtime_enabled=False,
            redis_url="redis://127.0.0.1:1/0",
        )
    )
    init = AsyncMock()
    monkeypatch.setattr(container.maintenance, "initialize", init)
    try:
        await container.start()
        init.assert_awaited_once()
        assert container.event_retention._task is not None
        assert await container.redis_reachable() is False
    finally:
        await container.stop()


async def test_silent_redis_does_not_hold_the_reachability_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = ApplicationContainer(
        Settings(_env_file=None, shared_storage_root=tmp_path)
    )
    monkeypatch.setattr(
        "executor_service.container.REDIS_PING_TIMEOUT_SECONDS", 0.05
    )
    monkeypatch.setattr(container.redis, "ping", lambda: asyncio.sleep(5))
    try:
        started = time.monotonic()
        assert await container.redis_reachable() is False
        assert time.monotonic() - started < 1
    finally:
        await container.redis.aclose()
        await container.engine.dispose()
