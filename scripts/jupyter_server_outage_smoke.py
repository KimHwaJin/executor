"""Stop and restore one local Jupyter server and verify fleet failover."""

import asyncio
import os
from uuid import uuid4

from mcp import Client
from redis.asyncio import Redis
from resilience_common import (
    available_port,
    cleanup_streams,
    execution,
    probe_runtime_target,
    start_executor,
    stop_executor,
    submit_static,
    upsert_runtime_target,
    wait_for_status,
    wait_ready,
)


async def _compose(*arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    decoded = output.decode(errors="replace")
    if process.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(arguments)} failed:\n{decoded}")
    return decoded


async def _wait_server_active(client: Client, server_id: str) -> dict[str, object]:
    for _ in range(160):
        server = await probe_runtime_target(client, server_id)
        if server["status"] == "ACTIVE":
            return server
        await asyncio.sleep(0.25)
    raise RuntimeError(f"Jupyter server {server_id} did not recover.")


async def main() -> None:
    if os.getenv("ALLOW_DOCKER_JUPYTER_OUTAGE_TEST") != "1":
        raise RuntimeError(
            "Set ALLOW_DOCKER_JUPYTER_OUTAGE_TEST=1 to permit the temporary local "
            "jupyter-secondary stop."
        )
    unique = uuid4().hex
    port = available_port("JUPYTER_OUTAGE_SMOKE_PORT")
    stream = f"executor.events.jupyter-outage-smoke.{unique}"
    group = f"executor-jupyter-outage-smoke-{unique}"
    process: asyncio.subprocess.Process | None = None
    secondary_restored = False
    try:
        await _compose(
            "--profile",
            "multi-jupyter",
            "up",
            "-d",
            "--wait",
            "jupyter",
            "jupyter-secondary",
        )
        process = await start_executor(
            port=port,
            consumer_name="jupyter-outage-smoke",
            stream=stream,
            group=group,
        )
        await wait_ready(port)
        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            primary = await upsert_runtime_target(
                client,
                unique=unique,
                name="local-jupyter",
                endpoint="http://127.0.0.1:8888",
                pool="INTERACTIVE",
                token=None,
            )
            secondary = await upsert_runtime_target(
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
            primary_id = str(primary["target_id"])
            secondary_id = str(secondary["target_id"])

            await _compose("stop", "jupyter-secondary")
            offline = await probe_runtime_target(client, secondary_id)
            if offline["status"] != "OFFLINE":
                raise RuntimeError(f"Stopped Jupyter server was not OFFLINE: {offline}")
            failover_id = await submit_static(
                client,
                unique=unique,
                name="jupyter-offline-failover",
                pool="INTERACTIVE",
                code="print('healthy server handled offline-peer workload')\n",
            )
            failover = await wait_for_status(
                client,
                failover_id,
                {"SUCCEEDED", "FAILED"},
            )
            if (
                failover["status"] != "SUCCEEDED"
                or str(failover["runtime_target_id"]) != primary_id
            ):
                raise RuntimeError(f"Work did not avoid the OFFLINE server: {failover}")

            await _compose(
                "--profile",
                "multi-jupyter",
                "up",
                "-d",
                "--wait",
                "jupyter-secondary",
            )
            secondary_restored = True
            recovered = await _wait_server_active(client, secondary_id)
            first_id = await submit_static(
                client,
                unique=unique,
                name="jupyter-recovered-first",
                pool="INTERACTIVE",
                code="import time\ntime.sleep(3)\nprint('first')\n",
            )
            second_id = await submit_static(
                client,
                unique=unique,
                name="jupyter-recovered-second",
                pool="INTERACTIVE",
                code="import time\ntime.sleep(3)\nprint('second')\n",
            )
            first_running, second_running = await asyncio.gather(
                wait_for_status(client, first_id, {"RUNNING"}, require_kernel=True),
                wait_for_status(client, second_id, {"RUNNING"}, require_kernel=True),
            )
            assigned_servers = {
                str(first_running["runtime_target_id"]),
                str(second_running["runtime_target_id"]),
            }
            if assigned_servers != {primary_id, secondary_id}:
                raise RuntimeError(
                    f"Recovered server did not rejoin scheduling: {assigned_servers}"
                )
            first, second = await asyncio.gather(
                wait_for_status(client, first_id, {"SUCCEEDED", "FAILED"}),
                wait_for_status(client, second_id, {"SUCCEEDED", "FAILED"}),
            )
            if first["status"] != "SUCCEEDED" or second["status"] != "SUCCEEDED":
                raise RuntimeError(f"Post-recovery executions failed: {first}, {second}")
            final_states = await asyncio.gather(
                execution(client, first_id),
                execution(client, second_id),
            )

        print("offline_status:", offline["status"])
        print("failover_status:", failover["status"])
        print("failover_server_id:", failover["runtime_target_id"])
        print("recovered_status:", recovered["status"])
        print("post_recovery_server_ids:", sorted(assigned_servers))
        print("post_recovery_statuses:", [state["status"] for state in final_states])
    finally:
        if not secondary_restored:
            await _compose(
                "--profile",
                "multi-jupyter",
                "up",
                "-d",
                "--wait",
                "jupyter-secondary",
            )
        await stop_executor(process)
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
