"""Async engine and session factory."""

from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool


def create_engine(
    database_url: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 5,
    pool_timeout_seconds: float = 30,
    pool_recycle_seconds: int = 1800,
    connect_timeout_seconds: int = 10,
    statement_timeout_seconds: int = 60,
    idle_in_transaction_timeout_seconds: int = 60,
    pooled: bool = True,
) -> AsyncEngine:
    """Engine whose sessions cannot hold a query or a transaction forever.

    Migrations set their own, longer transaction-local timeouts.
    ``pooled=False`` opens a fresh connection per use, for health checks
    that must not wait behind busy application connections.
    """
    options: dict[str, Any] = {}
    if not database_url.startswith("sqlite"):
        options = {
            "pool_pre_ping": True,
            "connect_args": {
                "connect_timeout": connect_timeout_seconds,
                "options": (
                    f"-c statement_timeout={statement_timeout_seconds}s "
                    "-c idle_in_transaction_session_timeout="
                    f"{idle_in_transaction_timeout_seconds}s"
                ),
            },
        }
        if pooled:
            options.update(
                pool_size=pool_size,
                max_overflow=max_overflow,
                pool_timeout=pool_timeout_seconds,
                pool_recycle=pool_recycle_seconds,
            )
        else:
            options["poolclass"] = NullPool
    return create_async_engine(database_url, hide_parameters=True, **options)


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
