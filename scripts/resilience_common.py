"""Shared helpers for real-process Executor resilience smoke tests."""

import asyncio
import os
import socket
import sys
from collections.abc import Mapping
from typing import Any

import httpx
from execution_spec_payload import execution_request, inline_spec
from mcp import Client
from redis.asyncio import Redis


async def require_exclusive_executor_control() -> None:
    """Fail fast when another local Executor can reconcile the shared PostgreSQL queue."""

    if os.getenv("RESILIENCE_ALLOW_CONCURRENT_EXECUTOR", "false").lower() in {
        "1",
        "true",
        "yes",
    }:
        return
    existing_url = os.getenv(
        "RESILIENCE_EXISTING_EXECUTOR_URL", "http://127.0.0.1:8000"
    ).rstrip("/")
    if not existing_url:
        return
    try:
        async with httpx.AsyncClient(timeout=0.75) as client:
            response = await client.get(f"{existing_url}/workerz")
    except httpx.HTTPError:
        return
    if response.status_code == 200:
        raise RuntimeError(
            "An unmanaged Executor is running against the shared queue at "
            f"{existing_url}. Stop it before this real-process resilience test "
            "(for Compose: `docker compose stop executor`). Concurrent Executors can "
            "claim the test rows through PostgreSQL reconciliation even when Redis Stream "
            "names differ. Set RESILIENCE_ALLOW_CONCURRENT_EXECUTOR=true only when the "
            "other process uses an isolated database."
        )


def available_port(environment_name: str) -> int:
    configured = os.getenv(environment_name)
    if configured is not None:
        return int(configured)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def start_executor(
    *,
    port: int,
    consumer_name: str,
    stream: str,
    group: str,
    extra_environment: Mapping[str, str] | None = None,
) -> asyncio.subprocess.Process:
    environment = os.environ.copy()
    environment.update(
        {
            "EXECUTION_CONSUMER_NAME": consumer_name,
            "EXECUTION_CONSUMER_GROUP": group,
            "EXECUTION_LEASE_SECONDS": "30",
            "EXECUTION_HEARTBEAT_SECONDS": "5",
            "RUNTIME_HEALTH_POLL_INTERVAL_SECONDS": "2",
            "RUNTIME_DEFAULT_MAX_CONCURRENT_EXECUTIONS": "1",
            "REDIS_WORK_STREAM": stream,
            "REDIS_EVENT_STREAM": f"{stream}.events",
            "REDIS_WORK_DEAD_LETTER_STREAM": f"{stream}.dlq",
            "REDIS_EVENT_DEAD_LETTER_STREAM": f"{stream}.events.dlq",
            "LOG_LEVEL": "WARNING",
        }
    )
    if extra_environment is not None:
        environment.update(extra_environment)
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uvicorn",
        "executor_service.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        env=environment,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=None,
    )


async def stop_executor(
    process: asyncio.subprocess.Process | None,
    *,
    timeout_seconds: float = 20,
) -> None:
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        async with asyncio.timeout(timeout_seconds):
            await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()


async def cleanup_streams(redis: Redis, stream: str) -> None:
    if os.getenv("RESILIENCE_KEEP_STREAMS", "false").lower() in {
        "1",
        "true",
        "yes",
    }:
        return
    await redis.delete(
        stream, f"{stream}.dlq", f"{stream}.events", f"{stream}.events.dlq"
    )


async def wait_ready(port: int, *, attempts: int = 160) -> None:
    async with httpx.AsyncClient() as client:
        for _ in range(attempts):
            try:
                response = await client.get(
                    f"http://127.0.0.1:{port}/readyz",
                    timeout=1,
                )
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
    raise RuntimeError(f"Executor on port {port} did not become ready.")


async def execution(client: Client, execution_id: str) -> dict[str, Any]:
    result = await client.call_tool(
        "execution_get", {"execution_id": execution_id}
    )
    if result.is_error:
        raise RuntimeError(str(result.content))
    return result.structured_content


async def attempts(client: Client, execution_id: str) -> list[dict[str, Any]]:
    result = await client.call_tool(
        "execution_attempt_list",
        {"execution_id": execution_id},
    )
    if result.is_error:
        raise RuntimeError(str(result.content))
    summaries = result.structured_content["items"]
    return await asyncio.gather(
        *(
            attempt_detail(client, execution_id, str(summary["attempt_id"]))
            for summary in summaries
        )
    )


async def attempt_detail(
    client: Client, execution_id: str, attempt_id: str
) -> dict[str, Any]:
    result = await client.call_tool(
        "execution_attempt_get",
        {"execution_id": execution_id, "attempt_id": attempt_id},
    )
    if result.is_error:
        raise RuntimeError(str(result.content))
    return result.structured_content


async def execution_steps(
    client: Client, execution_id: str
) -> list[dict[str, Any]]:
    result = await client.call_tool(
        "execution_step_list",
        {"execution_id": execution_id, "limit": 200},
    )
    if result.is_error:
        raise RuntimeError(str(result.content))
    return result.structured_content["items"]


async def attempt_steps(
    client: Client, execution_id: str, attempt_id: str
) -> list[dict[str, Any]]:
    result = await client.call_tool(
        "execution_attempt_step_list",
        {"execution_id": execution_id, "attempt_id": attempt_id, "limit": 200},
    )
    if result.is_error:
        raise RuntimeError(str(result.content))
    return result.structured_content["items"]


async def events(client: Client, execution_id: str) -> list[dict[str, Any]]:
    result = await client.call_tool(
        "execution_event_list",
        {"execution_id": execution_id},
    )
    if result.is_error:
        raise RuntimeError(str(result.content))
    return result.structured_content["items"]


async def wait_for_status(
    client: Client,
    execution_id: str,
    statuses: set[str],
    *,
    require_kernel: bool = False,
    attempts_count: int = 600,
    interval_seconds: float = 0.1,
) -> dict[str, Any]:
    for _ in range(attempts_count):
        state = await execution(client, execution_id)
        if state["state"]["status"] in statuses and (
            not require_kernel or state["runtime"]["session_id"] is not None
        ):
            return state
        await asyncio.sleep(interval_seconds)
    raise RuntimeError(f"Execution {execution_id} did not reach {statuses}.")


async def submit_static(
    client: Client,
    *,
    unique: str,
    name: str,
    pool: str,
    code: str,
) -> str:
    result = await client.call_tool(
        "execution_submit",
        {
            "request": execution_request(
                idempotency_key=f"resilience-{unique}-{name}",
                operation_mode="SINGLE",
                trigger_type="BATCH" if pool == "BATCH" else "INTERACTIVE",
                actor={
                    "type": "BATCH" if pool == "BATCH" else "USER",
                    "id": "resilience-batch"
                    if pool == "BATCH"
                    else "resilience-user",
                },
                runtime_profile="basic",
                spec=inline_spec(
                    [
                        {
                            "skill_name": "data_io",
                            "tool_name": name,
                            "code": code,
                        }
                    ],
                ),
                context={
                    "user_id": "resilience-user",
                    "project_id": "resilience-project",
                    "session_id": f"resilience-session-{unique}-{name}",
                    "task_id": f"resilience-task-{unique}-{name}",
                    "workflow_id": (
                        f"resilience-workflow-{unique}-{name}"
                        if pool == "BATCH"
                        else None
                    ),
                },
            )
        },
    )
    if result.is_error:
        raise RuntimeError(str(result.content))
    return str(result.structured_content["execution_id"])


async def upsert_runtime_target(
    client: Client,
    *,
    unique: str,
    name: str,
    endpoint: str,
    pool: str,
    token: str | None,
    capacity: int = 1,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "idempotency_key": f"resilience-server-{unique}-{name}",
        "name": name,
        "runtime_type": "JUPYTER",
        "connection_config": {"endpoint": endpoint},
        "pool": pool,
        "max_concurrent_executions": capacity,
        "actor": {"type": "USER", "id": "resilience-operator"},
    }
    if token is not None:
        request["credential"] = token
    result = await client.call_tool(
        "runtime_target_upsert", {"request": request}
    )
    if (
        result.is_error
        or result.structured_content["state"]["status"] != "ACTIVE"
    ):
        raise RuntimeError(
            f"Jupyter registration failed for {name}: {result.content}"
        )
    return result.structured_content


async def probe_runtime_target(
    client: Client, server_id: str
) -> dict[str, Any]:
    result = await client.call_tool(
        "runtime_target_probe",
        {
            "request": {
                "target_id": server_id,
                "actor": {"type": "USER", "id": "resilience-operator"},
            }
        },
    )
    if result.is_error:
        raise RuntimeError(str(result.content))
    return result.structured_content
