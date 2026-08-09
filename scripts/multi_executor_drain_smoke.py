"""Verify SIGTERM drain, timeout cleanup, and queued-work handoff."""

import asyncio
import os
from typing import Any
from uuid import uuid4

from mcp import Client
from redis.asyncio import Redis
from resilience_common import (
    attempts,
    available_port,
    cleanup_streams,
    start_executor,
    stop_executor,
    submit_static,
    upsert_jupyter_server,
    wait_for_status,
    wait_ready,
)

PRIMARY_CONSUMER = "drain-smoke-primary"
SECONDARY_CONSUMER = "drain-smoke-secondary"


def _attempt_owner(rows: list[dict[str, Any]]) -> str | None:
    if len(rows) != 1:
        return None
    return rows[0]["lease_owner"]


async def main() -> None:
    unique = uuid4().hex
    primary_port = available_port("DRAIN_SMOKE_PRIMARY_PORT")
    secondary_port = available_port("DRAIN_SMOKE_SECONDARY_PORT")
    if secondary_port == primary_port:
        raise ValueError("Drain smoke Executor ports must differ.")
    stream = f"executor.events.drain-smoke.{unique}"
    group = f"executor-drain-smoke-{unique}"
    primary: asyncio.subprocess.Process | None = None
    secondary: asyncio.subprocess.Process | None = None
    try:
        process_environment = {
            "EXECUTION_DRAIN_TIMEOUT_SECONDS": "6",
            "EXECUTION_SHUTDOWN_CLEANUP_SECONDS": "5",
        }
        primary = await start_executor(
            port=primary_port,
            consumer_name=PRIMARY_CONSUMER,
            stream=stream,
            group=group,
            extra_environment=process_environment,
        )
        await wait_ready(primary_port)
        async with Client(f"http://127.0.0.1:{primary_port}/mcp") as client:
            await upsert_jupyter_server(
                client,
                unique=unique,
                name="local-jupyter",
                endpoint="http://127.0.0.1:8888",
                pool="INTERACTIVE",
                token=None,
            )
            await upsert_jupyter_server(
                client,
                unique=unique,
                name="local-jupyter-secondary",
                endpoint="http://127.0.0.1:8889",
                pool="INTERACTIVE",
                token=os.getenv(
                    "JUPYTER_SECONDARY_TOKEN",
                    "change-me-secondary-local-only",
                ),
            )
            short_id = await submit_static(
                client,
                unique=unique,
                name="drain-short",
                pool="INTERACTIVE",
                code="import time\ntime.sleep(3)\nprint('short completed')\n",
            )
            long_id = await submit_static(
                client,
                unique=unique,
                name="drain-long",
                pool="INTERACTIVE",
                code="import time\ntime.sleep(30)\nprint('long completed')\n",
            )
            await wait_for_status(client, short_id, {"RUNNING"}, require_kernel=True)
            await wait_for_status(client, long_id, {"RUNNING"}, require_kernel=True)
            queued_id = await submit_static(
                client,
                unique=unique,
                name="drain-queued",
                pool="INTERACTIVE",
                code="print('queued completed')\n",
            )
            await wait_for_status(client, queued_id, {"QUEUED"})

        secondary = await start_executor(
            port=secondary_port,
            consumer_name=SECONDARY_CONSUMER,
            stream=stream,
            group=group,
            extra_environment=process_environment,
        )
        await wait_ready(secondary_port)
        primary.terminate()
        async with asyncio.timeout(20):
            await primary.wait()

        async with Client(f"http://127.0.0.1:{secondary_port}/mcp") as client:
            short = await wait_for_status(client, short_id, {"SUCCEEDED", "FAILED"})
            long = await wait_for_status(client, long_id, {"SUCCEEDED", "FAILED"})
            queued = await wait_for_status(client, queued_id, {"SUCCEEDED", "FAILED"})
            short_attempts = await attempts(client, short_id)
            long_attempts = await attempts(client, long_id)
            queued_attempts = await attempts(client, queued_id)

        if short["status"] != "SUCCEEDED":
            raise RuntimeError(f"Drain-window execution did not finish: {short}")
        if (
            long["status"] != "FAILED"
            or long["failure_type"] != "WORKER_SHUTDOWN"
            or long["retry_strategy"] != "FROM_START"
            or long["kernel_cleanup_status"] != "SUCCEEDED"
        ):
            raise RuntimeError(f"Drain-timeout execution was not recovered safely: {long}")
        if queued["status"] != "SUCCEEDED":
            raise RuntimeError(f"Queued execution was not handed off: {queued}")
        if _attempt_owner(short_attempts) != PRIMARY_CONSUMER:
            raise RuntimeError(f"Short execution owner changed unexpectedly: {short_attempts}")
        if _attempt_owner(long_attempts) != PRIMARY_CONSUMER:
            raise RuntimeError(f"Long execution owner changed unexpectedly: {long_attempts}")
        if _attempt_owner(queued_attempts) != SECONDARY_CONSUMER:
            raise RuntimeError(f"Queued execution was not claimed by secondary: {queued_attempts}")

        print("primary_exit_code:", primary.returncode)
        print("short_status:", short["status"])
        print("long_status:", long["status"])
        print("long_failure_type:", long["failure_type"])
        print("long_cleanup_status:", long["kernel_cleanup_status"])
        print("queued_status:", queued["status"])
        print("attempt_owners:", [
            _attempt_owner(short_attempts),
            _attempt_owner(long_attempts),
            _attempt_owner(queued_attempts),
        ])
    finally:
        await stop_executor(primary)
        await stop_executor(secondary)
        redis = Redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
        )
        try:
            await cleanup_streams(redis, stream)
        finally:
            await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
