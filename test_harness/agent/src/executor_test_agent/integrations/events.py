"""Redis Streams waiter used as the Executor-to-Agent wake-up channel."""

import asyncio
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from mcp import Client
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from executor_test_agent.integrations.contracts import ExecutionEventBatch, ExecutionEventEnvelope
from executor_test_agent.integrations.executor import required_tool_result

type EventHistoryLoader = Callable[[str, int, int], Awaitable[list[dict[str, Any]]]]

TERMINAL_EVENT_TYPES = {
    "execution.completed",
}

MULTI_OPERATION_WAKE_EVENT_TYPES = {
    "execution.operation_completed",
    "execution.completed",
}


class ExecutionEventWaiter:
    """Own one temporary consumer group so concurrent E2E runs cannot steal events."""

    def __init__(
        self,
        redis_url: str,
        stream: str,
        group_prefix: str,
        *,
        executor_mcp_url: str | None = None,
        history_loader: EventHistoryLoader | None = None,
        start_id: str = "$",
    ) -> None:
        suffix = uuid4().hex
        self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._stream = stream
        self._group = f"{group_prefix}-{suffix}"
        self._consumer = f"{socket.gethostname()}-{suffix}"
        self._start_id = start_id
        self._executor_mcp_url = executor_mcp_url
        self._history_loader = history_loader
        self._seen_event_ids: set[str] = set()

    async def open(self) -> None:
        try:
            await self._redis.xgroup_create(
                self._stream, self._group, id=self._start_id, mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def close(self) -> None:
        try:
            await self._redis.xgroup_destroy(self._stream, self._group)
        finally:
            await self._redis.aclose()

    async def wait_for_wakeup(
        self,
        execution_id: str,
        *,
        timeout_seconds: float,
        event_types: set[str] | None = None,
        operation_id: str | None = None,
        after_sequence: int = 0,
    ) -> ExecutionEventBatch:
        """Collect one Execution's events until the requested wake-up boundary."""

        accepted_event_types = event_types or TERMINAL_EVENT_TYPES
        collected: list[ExecutionEventEnvelope] = []
        last_sequence = after_sequence
        async with asyncio.timeout(timeout_seconds):
            while True:
                batches = await self._redis.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer,
                    streams={self._stream: ">"},
                    count=50,
                    block=1000,
                )
                for _, messages in batches:
                    for message_id, fields in messages:
                        if fields.get("execution_id") != execution_id:
                            await self._redis.xack(self._stream, self._group, message_id)
                            continue
                        event = ExecutionEventEnvelope.from_redis_fields(fields)
                        event_id = str(event.event_id)
                        if event_id in self._seen_event_ids:
                            await self._redis.xack(self._stream, self._group, message_id)
                            continue
                        if event.event_sequence <= last_sequence:
                            self._seen_event_ids.add(event_id)
                            await self._redis.xack(self._stream, self._group, message_id)
                            continue
                        if event.event_sequence > last_sequence + 1:
                            recovered = await self._recover_gap(
                                execution_id,
                                after_sequence=last_sequence,
                                before_sequence=event.event_sequence,
                            )
                            for recovered_event in recovered:
                                self._seen_event_ids.add(str(recovered_event.event_id))
                                last_sequence = recovered_event.event_sequence
                                if _matches_operation(recovered_event, operation_id):
                                    collected.append(recovered_event)
                        self._seen_event_ids.add(event_id)
                        if event.event_sequence != last_sequence + 1:
                            raise ValueError(
                                "Executor event history did not close the sequence gap."
                            )
                        last_sequence = event.event_sequence
                        if not _matches_operation(event, operation_id):
                            await self._redis.xack(self._stream, self._group, message_id)
                            continue
                        collected.append(event)
                        await self._redis.xack(self._stream, self._group, message_id)
                        if event.event_type in accepted_event_types:
                            return ExecutionEventBatch(events=collected, wake_event=event)

    async def _recover_gap(
        self,
        execution_id: str,
        *,
        after_sequence: int,
        before_sequence: int,
    ) -> list[ExecutionEventEnvelope]:
        recovered: list[ExecutionEventEnvelope] = []
        last_sequence = after_sequence
        while last_sequence + 1 < before_sequence:
            items = await self._load_history(
                execution_id,
                last_sequence,
                min(500, before_sequence - last_sequence - 1),
            )
            candidates = [
                ExecutionEventEnvelope.from_history_item(item)
                for item in items
                if int(item["event_sequence"]) < before_sequence
            ]
            if not candidates:
                raise ValueError("Executor event sequence gap could not be recovered.")
            for candidate in candidates:
                if candidate.event_sequence != last_sequence + 1:
                    raise ValueError("Executor event history is not contiguous.")
                recovered.append(candidate)
                last_sequence = candidate.event_sequence
        return recovered

    async def _load_history(
        self, execution_id: str, after_sequence: int, limit: int
    ) -> list[dict[str, Any]]:
        if self._history_loader is not None:
            return await self._history_loader(execution_id, after_sequence, limit)
        if self._executor_mcp_url is None:
            raise ValueError("executor_mcp_url is required for event gap recovery.")
        async with Client(self._executor_mcp_url) as client:
            page = await required_tool_result(
                client,
                "execution_event_list",
                {
                    "execution_id": execution_id,
                    "after_sequence": after_sequence,
                    "limit": limit,
                },
            )
        items = page.get("items")
        if not isinstance(items, list):
            raise ValueError("Executor event history response is invalid.")
        return [item for item in items if isinstance(item, dict)]


def _matches_operation(event: ExecutionEventEnvelope, operation_id: str | None) -> bool:
    if operation_id is None or event.event_type in TERMINAL_EVENT_TYPES:
        return True
    operation = event.payload.get("operation")
    payload_operation_id = (
        str(operation.get("id"))
        if isinstance(operation, dict) and operation.get("id") is not None
        else None
    )
    return payload_operation_id == operation_id


async def event_stream_watermark(redis_url: str, stream: str) -> str:
    """Return the latest Stream ID so a later consumer starts before a mutation's events."""

    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        rows = await redis.xrevrange(stream, count=1)
        return str(rows[0][0]) if rows else "0-0"
    finally:
        await redis.aclose()
