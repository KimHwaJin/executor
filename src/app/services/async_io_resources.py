"""Loop-local HTTP/DB pools shared by graphs and closed by their application host."""

from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakKeyDictionary

import httpx
from psycopg_pool import AsyncConnectionPool


@dataclass
class _Resources:
    http: httpx.AsyncClient | None = None
    pools: dict[str, AsyncConnectionPool] = field(default_factory=dict)


_by_loop: WeakKeyDictionary = WeakKeyDictionary()


def _resources() -> _Resources:
    loop = asyncio.get_running_loop()
    if loop not in _by_loop:
        _by_loop[loop] = _Resources()
    return _by_loop[loop]


def executor_http_client() -> httpx.AsyncClient:
    resources = _resources()
    if resources.http is None:
        resources.http = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            follow_redirects=True,
        )
    return resources.http


async def workflow_connection_pool(database_url: str) -> AsyncConnectionPool:
    resources = _resources()
    if database_url not in resources.pools:
        resources.pools[database_url] = AsyncConnectionPool(
            conninfo=database_url,
            open=False,
            min_size=0,
            max_size=int(os.environ.get("WORKFLOW_DB_POOL_MAX_SIZE", "8")),
            timeout=float(os.environ.get("WORKFLOW_DB_POOL_TIMEOUT_SECONDS", "10")),
            kwargs={
                "connect_timeout": int(os.environ.get("WORKFLOW_DB_CONNECT_TIMEOUT_SECONDS", "10")),
                "options": "-c statement_timeout=" + str(int(
                    os.environ.get("WORKFLOW_DB_STATEMENT_TIMEOUT_MS", "30000"))),
            },
        )
    pool = resources.pools[database_url]
    await pool.open()
    return pool


async def close_async_io_resources() -> None:
    """Call after graph tasks stop, before the host's event loop closes."""
    resources = _by_loop.pop(asyncio.get_running_loop(), None)
    if resources is None:
        return
    try:
        await asyncio.gather(*(pool.close() for pool in resources.pools.values()))
    finally:
        if resources.http is not None:
            await resources.http.aclose()


async def call_async(callback, *args: Any, **kwargs: Any) -> Any:
    """Await native async dependencies; offload legacy injected sync callbacks."""
    if inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(
        getattr(callback, "__call__", None)
    ):
        return await callback(*args, **kwargs)
    result = await asyncio.to_thread(callback, *args, **kwargs)
    return await result if inspect.isawaitable(result) else result
