"""Redis Streams waiter used as the Executor-to-Agent wake-up channel."""

import asyncio
import socket
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from executor_test_agent.integrations.contracts import ExecutionEventBatch, ExecutionEventEnvelope

TERMINAL_EVENT_TYPES = {
    "execution.succeeded",
    "execution.failed",
    "execution.cancelled",
}

MULTI_OPERATION_WAKE_EVENT_TYPES = {
    "execution.waiting_for_operation",
    "execution.failed",
    "execution.cancelled",
}


class ExecutionEventWaiter:
    """Own one temporary consumer group so concurrent E2E runs cannot steal events."""

    def __init__(
        self,
        redis_url: str,
        stream: str,
        group_prefix: str,
        *,
        start_id: str = "$",
    ) -> None:
        suffix = uuid4().hex
        self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._stream = stream
        self._group = f"{group_prefix}-{suffix}"
        self._consumer = f"{socket.gethostname()}-{suffix}"
        self._start_id = start_id
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
    ) -> ExecutionEventBatch:
        """Collect one Execution's events until the requested wake-up boundary."""

        accepted_event_types = event_types or TERMINAL_EVENT_TYPES
        collected: list[ExecutionEventEnvelope] = []
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
                        if fields.get("aggregate_id") != execution_id:
                            await self._redis.xack(self._stream, self._group, message_id)
                            continue
                        event = ExecutionEventEnvelope.from_redis_fields(fields)
                        await self._redis.xack(self._stream, self._group, message_id)
                        event_id = str(event.event_id)
                        if event_id in self._seen_event_ids:
                            continue
                        self._seen_event_ids.add(event_id)
                        payload_operation_id = event.payload.get("operation_id")
                        if (
                            operation_id is not None
                            and event.event_type not in TERMINAL_EVENT_TYPES
                            and payload_operation_id != operation_id
                        ):
                            continue
                        collected.append(event)
                        if event.event_type in accepted_event_types:
                            return ExecutionEventBatch(events=collected, wake_event=event)


async def event_stream_watermark(redis_url: str, stream: str) -> str:
    """Return the latest Stream ID so a later consumer starts before a mutation's events."""

    redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        rows = await redis.xrevrange(stream, count=1)
        return str(rows[0][0]) if rows else "0-0"
    finally:
        await redis.aclose()
