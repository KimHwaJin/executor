"""LangGraph API lifespan for the Executor binding store."""

from __future__ import annotations

from contextlib import asynccontextmanager

from starlette.applications import Starlette

from app.agent_worker.api_bridge import get_api_worker_bridge
from app.services.async_io_resources import close_async_io_resources


@asynccontextmanager
async def lifespan(_: Starlette):
    try:
        async with get_api_worker_bridge():
            yield
    finally:
        await close_async_io_resources()


app = Starlette(lifespan=lifespan)


__all__ = ["app"]
