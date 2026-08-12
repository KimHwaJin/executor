"""Cross-platform event-loop helpers for psycopg async connections."""

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any


def run_async[T](coroutine: Coroutine[Any, Any, T], *, platform: str | None = None) -> T:
    """Run a coroutine on a psycopg-compatible loop, including native Windows."""
    current_platform = sys.platform if platform is None else platform
    if current_platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(coroutine)
    return asyncio.run(coroutine)
