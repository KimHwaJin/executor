"""Redis Streams waiter used as the Executor-to-Agent wake-up channel."""

import asyncio
import socket
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from executor_test_agent.integrations.contracts import ExecutionEventEnvelope

TERMINAL_EVENT_TYPES = {
    "execution.succeeded",
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
        include_existing: bool = False,
    ) -> None:
        suffix = uuid4().hex
        self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._stream = stream
        self._group = f"{group_prefix}-{suffix}"
        self._consumer = f"{socket.gethostname()}-{suffix}"
        self._start_id = "0-0" if include_existing else "$"

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

    async def wait_for_terminal(
        self, execution_id: str, *, timeout_seconds: float
    ) -> ExecutionEventEnvelope:
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
                        event = ExecutionEventEnvelope.from_redis_fields(fields)
                        await self._redis.xack(self._stream, self._group, message_id)
                        if (
                            str(event.aggregate_id) == execution_id
                            and event.event_type in TERMINAL_EVENT_TYPES
                        ):
                            return event
