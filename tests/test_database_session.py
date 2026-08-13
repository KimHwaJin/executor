"""Database engine pool configuration tests."""

from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.config import Settings
from executor_service.infrastructure.db import session as session_module


def test_postgresql_engine_uses_bounded_queue_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    expected = cast(AsyncEngine, object())

    def fake_create_async_engine(database_url: str, **options: Any) -> AsyncEngine:
        captured["database_url"] = database_url
        captured.update(options)
        return expected

    monkeypatch.setattr(session_module, "create_async_engine", fake_create_async_engine)

    actual = session_module.create_engine(
        "postgresql+psycopg://executor:secret@postgres/executor",
        pool_size=12,
        max_overflow=4,
        pool_timeout_seconds=7.5,
        pool_recycle_seconds=900,
        connect_timeout_seconds=3,
    )

    assert actual is expected
    assert captured == {
        "database_url": "postgresql+psycopg://executor:secret@postgres/executor",
        "pool_pre_ping": True,
        "pool_size": 12,
        "max_overflow": 4,
        "pool_timeout": 7.5,
        "pool_recycle": 900,
        "connect_args": {"connect_timeout": 3},
    }


def test_sqlite_engine_ignores_postgresql_pool_options(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    expected = cast(AsyncEngine, object())

    def fake_create_async_engine(database_url: str, **options: Any) -> AsyncEngine:
        captured["database_url"] = database_url
        captured.update(options)
        return expected

    monkeypatch.setattr(session_module, "create_async_engine", fake_create_async_engine)

    actual = session_module.create_engine(
        "sqlite+aiosqlite:///:memory:",
        pool_size=12,
        max_overflow=4,
        pool_timeout_seconds=7.5,
        pool_recycle_seconds=900,
        connect_timeout_seconds=3,
    )

    assert actual is expected
    assert captured == {"database_url": "sqlite+aiosqlite:///:memory:"}


def test_database_pool_settings_reject_invalid_capacity() -> None:
    with pytest.raises(ValueError, match="database_pool_size"):
        Settings(database_pool_size=0)
