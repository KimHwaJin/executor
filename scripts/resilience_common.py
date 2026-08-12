"""Shared helpers for real-process Executor resilience smoke tests."""

import asyncio
import os
import socket
import sys
from collections.abc import Mapping
from typing import Any

import httpx
from execution_spec_payload import inline_source
from mcp import Client
from redis.asyncio import Redis


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
            "REDIS_STREAM": stream,
            "REDIS_DEAD_LETTER_STREAM": f"{stream}.dlq",
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
    if os.getenv("RESILIENCE_KEEP_STREAMS", "false").lower() in {"1", "true", "yes"}:
        return
    await redis.delete(stream, f"{stream}.dlq")


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
    result = await client.call_tool("execution_get", {"execution_id": execution_id})
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
        if state["status"] in statuses and (
            not require_kernel or state["runtime_session_id"] is not None
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
            "request": {
                "idempotency_key": f"resilience-{unique}-{name}",
                "mode": "STATIC",
                "trigger_type": "BATCH" if pool == "BATCH" else "INTERACTIVE",
                "actor": {
                    "type": "BATCH" if pool == "BATCH" else "USER",
                    "id": "resilience-batch" if pool == "BATCH" else "resilience-user",
                },
                "runtime_profile": "python3",
                "source": inline_source(
                    f"resilience-plan-{unique}-{name}",
                    [{"skill_name": "data_io", "tool_name": name, "code": code}],
                ),
                "context": {
                    "user_id": "resilience-user",
                    "project_id": "resilience-project",
                    "session_id": f"resilience-session-{unique}-{name}",
                    "task_id": f"resilience-task-{unique}-{name}",
                },
            }
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
    result = await client.call_tool("runtime_target_upsert", {"request": request})
    if result.is_error or result.structured_content["status"] != "ACTIVE":
        raise RuntimeError(f"Jupyter registration failed for {name}: {result.content}")
    return result.structured_content


async def probe_runtime_target(client: Client, server_id: str) -> dict[str, Any]:
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
