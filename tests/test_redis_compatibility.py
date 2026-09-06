"""Validation and startup contracts, without external Redis."""

from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from executor_service.container import ApplicationContainer
from executor_service.infrastructure.redis_streams import (
    check_redis_compatibility,
    claim_pending,
    next_stream_id,
    trim_before,
)
from executor_service.settings import Settings


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0-0", "0-1"),
        ("123-9007199254740993", "123-9007199254740994"),
        ("123-18446744073709551615", "124-0"),
        ("18446744073709551615-18446744073709551615", "0-0"),
    ],
)
def test_cursor_preserves_uint64_precision(value: str, expected: str) -> None:
    assert next_stream_id(value) == expected


@pytest.mark.parametrize(
    "value",
    ["-", "(1-0", "1", "-1-0", "1--1", "1-18446744073709551616", "\uff11-0"],
)
def test_invalid_stream_id_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        next_stream_id(value)


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
        await claim_pending(
            cast(Redis, redis), "s", "g", "c", min_idle_ms=1, count=count
        )
    with pytest.raises(ValueError):
        await trim_before(
            cast(Redis, redis), "s", "100-0", protect_groups=True, count=count
        )
    redis.execute_command.assert_not_awaited()


async def test_unsupported_redis_prevents_background_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = ApplicationContainer(
        Settings(
            _env_file=None, shared_storage_root=tmp_path, db_auto_migrate=False
        )
    )
    check = AsyncMock(side_effect=RuntimeError("unsupported Redis"))
    init = AsyncMock()
    monkeypatch.setattr(
        "executor_service.container.check_redis_compatibility", check
    )
    monkeypatch.setattr(container.maintenance, "initialize", init)
    try:
        with pytest.raises(RuntimeError, match="unsupported Redis"):
            await container.start()
        init.assert_not_awaited()
        assert container.event_retention._task is None
    finally:
        await container.stop()
