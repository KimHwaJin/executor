"""Pluggable completion signals for Executor Operations."""

from __future__ import annotations

import asyncio
import time

from app.services.async_io_resources import call_async
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from agent_config import AgentSettings
from app.services.executor_client import get_execution


@dataclass(frozen=True)
class ExecutionSignal:
    execution_id: str
    status: str
    state_version: int
    response: dict[str, Any]


class ExecutionWaiter(Protocol):
    async def wait(self, execution_id: str, *, operation_mode: str) -> ExecutionSignal:
        ...


class ExecutionPollingTimeout(TimeoutError):
    pass


class ExecutionPollingFailed(RuntimeError):
    pass


class PollingExecutionWaiter:
    """Poll PostgreSQL-backed execution state until the current work is done."""

    def __init__(
        self,
        settings: AgentSettings,
        *,
        fetch_execution: Callable[[AgentSettings, str], dict[str, Any]] = get_execution,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.fetch_execution = fetch_execution
        self.monotonic = monotonic
        self.sleep = sleep

    async def wait(self, execution_id: str, *, operation_mode: str) -> ExecutionSignal:
        deadline = self.monotonic() + self.settings.executor_poll_timeout_seconds
        last_status = "UNKNOWN"
        while True:
            response = await call_async(self.fetch_execution, self.settings, execution_id)
            body = response.get("body") or {}
            state = body.get("state") or {}
            status = str(state.get("status") or "UNKNOWN")
            last_status = status
            version = int(state.get("version", 0))

            if operation_mode == "MULTI" and status == "WAITING_FOR_OPERATION":
                return ExecutionSignal(execution_id, status, version, body)
            if operation_mode == "SINGLE" and status in {
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
            }:
                return ExecutionSignal(execution_id, status, version, body)
            if operation_mode == "MULTI" and status in {"FAILED", "CANCELLED"}:
                failure = body.get("failure") or {}
                raise ExecutionPollingFailed(
                    f"MULTI Execution {execution_id} ended with {status} "
                    f"({failure.get('type') or 'UNKNOWN_FAILURE'}): "
                    f"{failure.get('message') or 'no failure message'}"
                )
            if self.monotonic() >= deadline:
                raise ExecutionPollingTimeout(
                    f"Execution {execution_id} did not become ready within "
                    f"{self.settings.executor_poll_timeout_seconds}s "
                    f"(last status: {last_status})"
                )
            await call_async(self.sleep, self.settings.executor_poll_interval_seconds)


class RedisExecutionWaiter:
    """Injection boundary for the future Redis Streams event consumer."""

    async def wait(self, execution_id: str, *, operation_mode: str) -> ExecutionSignal:
        raise NotImplementedError(
            "RedisExecutionWaiter is not connected yet; inject the Redis consumer "
            "implementation at graph construction time."
        )


__all__ = [
    "ExecutionPollingFailed",
    "ExecutionPollingTimeout",
    "ExecutionSignal",
    "ExecutionWaiter",
    "PollingExecutionWaiter",
    "RedisExecutionWaiter",
]

