"""LangGraph API lifecycle for the shared PostgreSQL checkpointer."""

from __future__ import annotations

import os
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from agent_config import load_agent_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def create_checkpointer(
    database_url: str | None = None,
    *,
    setup_on_start: bool | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    timeout: float | None = None,
) -> AsyncGenerator[AsyncPostgresSaver]:
    settings = load_agent_settings()
    database_url = (
        database_url if database_url is not None
        else os.environ.get("AGENT_CHECKPOINT_DATABASE_URL", "")
    ).strip()
    if not database_url:
        raise RuntimeError("AGENT_CHECKPOINT_DATABASE_URL is required")

    min_size = settings.checkpoint_pool_min_size if min_size is None else min_size
    max_size = settings.checkpoint_pool_max_size if max_size is None else max_size
    timeout = settings.checkpoint_pool_timeout_seconds if timeout is None else timeout
    if not 1 <= min_size <= max_size or not 0 < timeout < float("inf"):
        raise ValueError("Invalid checkpoint pool size or timeout")
    if setup_on_start is None:
        setup_on_start = settings.checkpoint_setup_on_start
    pool = AsyncConnectionPool(
        conninfo=database_url,
        name="agent-checkpoint",
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        open=False,
        check=AsyncConnectionPool.check_connection,
        kwargs={"autocommit": True, "row_factory": dict_row, "connect_timeout": 10},
    )
    try:
        await pool.open(wait=True, timeout=timeout)
        checkpointer = AsyncPostgresSaver(pool)
        logger.warning(
            "Checkpoint created: saver=%s connection=%s pool_stats=%s",
            type(checkpointer).__name__,
            type(checkpointer.conn).__name__,
            pool.get_stats(),
        )
        if setup_on_start:
            await checkpointer.setup()
        yield checkpointer
    finally:
        logger.info("Checkpoint pool closing: %s", pool.get_stats())
        await pool.close()


__all__ = ["create_checkpointer"]