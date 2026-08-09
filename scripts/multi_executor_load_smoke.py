"""Run 20-30 concurrent executions and verify distributed capacity invariants."""

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
    execution,
    start_executor,
    stop_executor,
    submit_static,
    upsert_jupyter_server,
    wait_ready,
)

PRIMARY_CONSUMER = "load-smoke-primary"
SECONDARY_CONSUMER = "load-smoke-secondary"
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


async def _servers(client: Client) -> list[dict[str, Any]]:
    result = await client.call_tool("jupyter_server_list", {})
    if result.is_error:
        raise RuntimeError(str(result.content))
    return result.structured_content["result"]


async def _probe(client: Client, server_id: str) -> dict[str, Any]:
    result = await client.call_tool("jupyter_server_probe", {"server_id": server_id})
    if result.is_error:
        raise RuntimeError(str(result.content))
    return result.structured_content


async def main() -> None:
    unique = uuid4().hex
    execution_count = int(os.getenv("RESILIENCE_EXECUTION_COUNT", "30"))
    if not 20 <= execution_count <= 60:
        raise ValueError("RESILIENCE_EXECUTION_COUNT must be between 20 and 60.")
    cell_sleep_seconds = float(os.getenv("RESILIENCE_CELL_SLEEP_SECONDS", "1"))
    primary_port = available_port("LOAD_SMOKE_PRIMARY_PORT")
    secondary_port = available_port("LOAD_SMOKE_SECONDARY_PORT")
    if secondary_port == primary_port:
        raise ValueError("Load smoke Executor ports must differ.")
    stream = f"executor.events.load-smoke.{unique}"
    group = f"executor-load-smoke-{unique}"
    primary: asyncio.subprocess.Process | None = None
    secondary: asyncio.subprocess.Process | None = None
    try:
        primary = await start_executor(
            port=primary_port,
            consumer_name=PRIMARY_CONSUMER,
            stream=stream,
            group=group,
        )
        await wait_ready(primary_port)
        secondary = await start_executor(
            port=secondary_port,
            consumer_name=SECONDARY_CONSUMER,
            stream=stream,
            group=group,
        )
        await wait_ready(secondary_port)

        async with Client(f"http://127.0.0.1:{primary_port}/mcp") as client:
            specs = (
                (
                    "local-jupyter",
                    "http://127.0.0.1:8888",
                    "INTERACTIVE",
                    None,
                ),
                (
                    "local-jupyter-secondary",
                    "http://127.0.0.1:8889",
                    "INTERACTIVE",
                    os.getenv(
                        "JUPYTER_SECONDARY_TOKEN",
                        "change-me-secondary-local-only",
                    ),
                ),
                (
                    "local-jupyter-batch-primary",
                    "http://127.0.0.1:8890",
                    "BATCH",
                    os.getenv(
                        "JUPYTER_BATCH_PRIMARY_TOKEN",
                        "change-me-batch-primary-local-only",
                    ),
                ),
                (
                    "local-jupyter-batch-secondary",
                    "http://127.0.0.1:8891",
                    "BATCH",
                    os.getenv(
                        "JUPYTER_BATCH_SECONDARY_TOKEN",
                        "change-me-batch-secondary-local-only",
                    ),
                ),
            )
            registered = [
                await upsert_jupyter_server(
                    client,
                    unique=unique,
                    name=name,
                    endpoint=endpoint,
                    pool=pool,
                    token=token,
                    capacity=1,
                )
                for name, endpoint, pool, token in specs
            ]
            server_ids = {str(item["server_id"]) for item in registered}
            capacities = {
                str(item["server_id"]): int(item["max_concurrent_executions"])
                for item in registered
            }
            execution_ids: list[str] = []
            interactive_count = execution_count // 2
            for index in range(execution_count):
                pool = "INTERACTIVE" if index < interactive_count else "BATCH"
                execution_ids.append(
                    await submit_static(
                        client,
                        unique=unique,
                        name=f"load-{index:02d}",
                        pool=pool,
                        code=(
                            "import time\n"
                            f"time.sleep({cell_sleep_seconds})\n"
                            f"print('load-{index:02d}')\n"
                        ),
                    )
                )

            peak_active = {server_id: 0 for server_id in server_ids}
            queued_observed = False
            final_states: dict[str, dict[str, Any]] = {}
            for _ in range(600):
                states = await asyncio.gather(
                    *(execution(client, execution_id) for execution_id in execution_ids)
                )
                final_states = {
                    execution_id: state
                    for execution_id, state in zip(execution_ids, states, strict=True)
                }
                queued_observed = queued_observed or any(
                    state["status"] == "QUEUED" for state in states
                )
                for server in await _servers(client):
                    server_id = str(server["server_id"])
                    if server_id not in server_ids:
                        continue
                    active = int(server["active_execution_count"])
                    peak_active[server_id] = max(peak_active[server_id], active)
                    if active > capacities[server_id]:
                        raise RuntimeError(
                            f"Jupyter capacity exceeded for {server['name']}: "
                            f"active={active}, capacity={capacities[server_id]}"
                        )
                if all(state["status"] in TERMINAL_STATUSES for state in states):
                    break
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("Concurrent executions did not finish in time.")

            failed = {
                execution_id: state
                for execution_id, state in final_states.items()
                if state["status"] != "SUCCEEDED"
            }
            if failed:
                raise RuntimeError(f"Concurrent executions failed: {failed}")
            all_attempts = await asyncio.gather(
                *(attempts(client, execution_id) for execution_id in execution_ids)
            )
            duplicate_attempts = [
                execution_id
                for execution_id, rows in zip(execution_ids, all_attempts, strict=True)
                if len(rows) != 1 or rows[0]["status"] != "SUCCEEDED"
            ]
            if duplicate_attempts:
                raise RuntimeError(f"Unexpected Attempt history: {duplicate_attempts}")
            owners = {str(rows[0]["lease_owner"]) for rows in all_attempts}
            if owners != {PRIMARY_CONSUMER, SECONDARY_CONSUMER}:
                raise RuntimeError(f"Both Executor processes were not used: {owners}")
            if not queued_observed:
                raise RuntimeError("Load test never observed capacity-backed QUEUED work.")

            probes = await asyncio.gather(*(_probe(client, server_id) for server_id in server_ids))
            leaked = [
                server
                for server in probes
                if server["active_execution_count"] != 0
                or server["active_kernel_count"] != 0
            ]
            if leaked:
                raise RuntimeError(f"Active Attempt or kernel remained after load: {leaked}")

        print("execution_count:", execution_count)
        print("successful_count:", len(final_states))
        print("queued_observed:", queued_observed)
        print("attempt_owners:", sorted(owners))
        print("peak_active_by_server:", peak_active)
        print("capacity_by_server:", capacities)
        print("leaked_kernel_count:", 0)
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
