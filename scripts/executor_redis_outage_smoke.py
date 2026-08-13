"""Verify PostgreSQL reconciliation while Redis command processing is paused."""

import asyncio
import os
from time import monotonic
from uuid import uuid4

from mcp import Client
from redis.asyncio import Redis
from resilience_common import (
    available_port,
    cleanup_streams,
    events,
    start_executor,
    stop_executor,
    submit_static,
    upsert_runtime_target,
    wait_for_status,
    wait_ready,
)


async def main() -> None:
    unique = uuid4().hex
    port = available_port("REDIS_OUTAGE_SMOKE_PORT")
    pause_milliseconds = int(os.getenv("REDIS_OUTAGE_PAUSE_MILLISECONDS", "8000"))
    if pause_milliseconds < 5000:
        raise ValueError("REDIS_OUTAGE_PAUSE_MILLISECONDS must be at least 5000.")
    stream = f"executor.events.redis-outage-smoke.{unique}"
    group = f"executor-redis-outage-smoke-{unique}"
    process: asyncio.subprocess.Process | None = None
    pause_started: float | None = None
    redis = Redis.from_url(
        os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
        decode_responses=True,
    )
    try:
        process = await start_executor(
            port=port,
            consumer_name="redis-outage-smoke",
            stream=stream,
            group=group,
            extra_environment={"OUTBOX_POLL_INTERVAL_SECONDS": "0.1"},
        )
        await wait_ready(port)
        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            await upsert_runtime_target(
                client,
                unique=unique,
                name="local-jupyter",
                endpoint="http://127.0.0.1:8888",
                pool="INTERACTIVE",
                token=None,
            )
            pause_started = monotonic()
            await redis.execute_command(
                "CLIENT",
                "PAUSE",
                pause_milliseconds,
                "ALL",
            )
            pause_deadline = pause_started + pause_milliseconds / 1000
            execution_id = await submit_static(
                client,
                unique=unique,
                name="redis-paused-reconciliation",
                pool="INTERACTIVE",
                code="print('postgresql reconciliation completed execution')\n",
            )
            succeeded = await wait_for_status(
                client,
                execution_id,
                {"SUCCEEDED", "FAILED"},
                attempts_count=60,
                interval_seconds=0.1,
            )
            completed_at = monotonic()
            if succeeded["state"]["status"] != "SUCCEEDED":
                raise RuntimeError(f"Execution failed during Redis outage: {succeeded}")
            if completed_at >= pause_deadline:
                raise RuntimeError(
                    "Execution did not complete until after Redis resumed; "
                    "PostgreSQL reconciliation was not demonstrated."
                )

            await asyncio.sleep(max(0, pause_deadline - monotonic()) + 0.5)
            timeline = []
            for _ in range(100):
                timeline = await events(client, execution_id)
                if timeline and all(
                    event["delivery"]["status"] == "PUBLISHED" for event in timeline
                ):
                    break
                await asyncio.sleep(0.1)
            else:
                raise RuntimeError(f"Outbox did not recover after Redis resumed: {timeline}")
            stream_entries = await redis.xrange(stream)
            if not stream_entries:
                raise RuntimeError("Recovered Outbox did not publish to Redis Stream.")

        print("execution_id:", execution_id)
        print("execution_status:", succeeded["state"]["status"])
        print("completed_while_redis_paused:", True)
        print("redis_pause_milliseconds:", pause_milliseconds)
        print("outbox_event_count:", len(timeline))
        print("all_events_published:", True)
        print("stream_entry_count:", len(stream_entries))
    finally:
        if pause_started is not None:
            remaining_pause = pause_milliseconds / 1000 - (monotonic() - pause_started)
            if remaining_pause > 0:
                await asyncio.sleep(remaining_pause + 0.1)
        await stop_executor(process)
        try:
            await cleanup_streams(redis, stream)
        finally:
            await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
