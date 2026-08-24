"""Async engine and session factory."""

from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(
    database_url: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 5,
    pool_timeout_seconds: float = 30,
    pool_recycle_seconds: int = 1800,
    connect_timeout_seconds: int = 10,
) -> AsyncEngine:
    options: dict[str, Any] = {}
    if not database_url.startswith("sqlite"):
        options = {
            "pool_pre_ping": True,
            "pool_size": pool_size,
            "max_overflow": max_overflow,
            "pool_timeout": pool_timeout_seconds,
            "pool_recycle": pool_recycle_seconds,
            "connect_args": {"connect_timeout": connect_timeout_seconds},
        }
    return create_async_engine(database_url, **options)


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
