"""Redis Streams transport for durable execution work messages."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from executor_service.domain.models import utc_now
from executor_service.infrastructure.execution_worker.message_validation import (
    invalid_work_message_reason,
    valid_uuid_or_empty,
)
from executor_service.settings import Settings

logger = logging.getLogger(__name__)

WorkMessageHandler = Callable[[dict[str, str]], Awaitable[bool]]


class WorkStreamConsumer:
    """Consumes, reclaims, acknowledges, and dead-letters work messages."""

    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        consumer_name: str,
        handler: WorkMessageHandler,
    ) -> None:
        self._redis = redis
        self._settings = settings
        self._consumer_name = consumer_name
        self._handler = handler
        self._pending_claim_cursor = "0-0"

    async def ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                self._settings.redis_work_stream,
                self._settings.execution_consumer_group,
                id="0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                batches = await self._redis.xreadgroup(
                    groupname=self._settings.execution_consumer_group,
                    consumername=self._consumer_name,
                    streams={self._settings.redis_work_stream: ">"},
                    count=20,
                    block=1000,
                )
                for _stream, messages in batches:
                    for message_id, fields in messages:
                        await self.process_message(message_id, fields)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Execution stream consumer failed")
                await asyncio.sleep(1)

    async def pending_recovery_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.recover_pending_messages()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Execution pending-message recovery failed")
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=(
                        self._settings.execution_pending_claim_interval_seconds
                    ),
                )
            except TimeoutError:
                pass

    async def recover_pending_messages(self) -> int:
        result = await self._redis.xautoclaim(
            self._settings.redis_work_stream,
            self._settings.execution_consumer_group,
            self._consumer_name,
            min_idle_time=(
                self._settings.execution_pending_claim_idle_milliseconds
            ),
            start_id=self._pending_claim_cursor,
            count=self._settings.execution_pending_claim_batch_size,
        )
        next_cursor = result[0]
        messages = result[1]
        self._pending_claim_cursor = str(next_cursor)
        reclaimed = 0
        for message_id, fields in messages:
            reclaimed += 1
            await self.process_message(message_id, fields)
        return reclaimed

    async def process_message(
        self,
        message_id: str,
        fields: dict[str, str],
    ) -> None:
        invalid_reason = invalid_work_message_reason(fields)
        if invalid_reason is not None:
            try:
                await self._dead_letter(message_id, fields, invalid_reason)
                await self._ack(message_id)
            except Exception:
                logger.exception(
                    "Execution work message DLQ delivery failed",
                    extra={"message_id": message_id, "reason": invalid_reason},
                )
            return
        try:
            await self._handler(fields)
        except Exception:
            logger.exception(
                "Execution work message handling failed",
                extra={"message_id": message_id},
            )
            return
        await self._ack(message_id)

    async def _ack(self, message_id: str) -> None:
        await self._redis.xack(
            self._settings.redis_work_stream,
            self._settings.execution_consumer_group,
            message_id,
        )

    async def _dead_letter(
        self,
        message_id: str,
        fields: dict[str, str],
        reason: str,
    ) -> None:
        await self._redis.xadd(
            self._settings.redis_work_dead_letter_stream,
            {
                "source_stream": self._settings.redis_work_stream,
                "source_message_id": message_id,
                "message_id": valid_uuid_or_empty(fields.get("message_id")),
                "aggregate_id": valid_uuid_or_empty(
                    fields.get("aggregate_id")
                ),
                "reason": reason,
                "dead_lettered_at": utc_now().isoformat(),
            },
        )
