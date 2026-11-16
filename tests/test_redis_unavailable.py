"""Startup and reachability contracts, without external Redis."""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from executor_service.container import ApplicationContainer
from executor_service.settings import Settings


async def test_startup_does_not_need_redis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = ApplicationContainer(
        Settings(
            _env_file=None,
            storage_root=tmp_path,
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
        Settings(_env_file=None, storage_root=tmp_path)
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
