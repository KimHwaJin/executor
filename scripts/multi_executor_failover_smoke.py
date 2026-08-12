"""Verify crash failover between two Executor processes against real infrastructure."""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from execution_spec_payload import inline_source
from mcp import Client
from redis.asyncio import Redis

PRIMARY_CONSUMER = "multi-smoke-primary"
SECONDARY_CONSUMER = "multi-smoke-secondary"


async def _start_executor(
    *, port: int, consumer_name: str, stream: str, group: str
) -> asyncio.subprocess.Process:
    environment = os.environ.copy()
    environment.update(
        {
            "EXECUTION_CONSUMER_NAME": consumer_name,
            "EXECUTION_CONSUMER_GROUP": group,
            "EXECUTION_LEASE_SECONDS": "30",
            "EXECUTION_HEARTBEAT_SECONDS": "5",
            "REDIS_STREAM": stream,
            "REDIS_DEAD_LETTER_STREAM": f"{stream}.dlq",
            "LOG_LEVEL": "WARNING",
        }
    )
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
        stderr=asyncio.subprocess.DEVNULL,
    )


async def _stop_executor(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        async with asyncio.timeout(10):
            await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()


async def _wait_ready(port: int) -> None:
    async with httpx.AsyncClient() as client:
        for _ in range(120):
            try:
                response = await client.get(f"http://127.0.0.1:{port}/readyz")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
    raise RuntimeError(f"Executor on port {port} did not become ready.")


async def _execution(client: Client, execution_id: str) -> dict[str, Any]:
    result = await client.call_tool("execution_get", {"execution_id": execution_id})
    if result.is_error:
        raise RuntimeError(str(result.content))
    return result.structured_content


async def _wait_for(
    client: Client,
    execution_id: str,
    statuses: set[str],
    *,
    require_kernel: bool = False,
    attempts: int = 300,
) -> dict[str, Any]:
    for _ in range(attempts):
        state = await _execution(client, execution_id)
        if state["status"] in statuses and (
            not require_kernel or state["runtime_session_id"] is not None
        ):
            return state
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Execution {execution_id} did not reach {statuses}.")


async def _attempts(client: Client, execution_id: str) -> list[dict[str, Any]]:
    result = await client.call_tool("execution_attempt_list", {"execution_id": execution_id})
    if result.is_error:
        raise RuntimeError(str(result.content))
    return result.structured_content["items"]


async def _wait_for_recovered_failure(client: Client, execution_id: str) -> dict[str, Any]:
    for _ in range(300):
        state = await _execution(client, execution_id)
        if state["status"] == "FAILED" and state["runtime_session_cleanup_status"] != "PENDING":
            return state
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Execution {execution_id} did not finish crash cleanup.")


async def main() -> None:
    unique = uuid4().hex
    primary_port = int(os.getenv("MULTI_EXECUTOR_PRIMARY_PORT", "8010"))
    secondary_port = int(os.getenv("MULTI_EXECUTOR_SECONDARY_PORT", "8011"))
    stream = f"executor.events.multi-smoke.{unique}"
    group = f"executor-multi-smoke-{unique}"
    primary: asyncio.subprocess.Process | None = None
    secondary: asyncio.subprocess.Process | None = None
    execution_id = ""
    marker = Path("checkpoints") / f"multi-executor-{unique}.marker"
    code = (
        "from pathlib import Path\n"
        "import time\n"
        f"marker = Path('{marker.as_posix()}')\n"
        "marker.parent.mkdir(parents=True, exist_ok=True)\n"
        "if not marker.exists():\n"
        "    marker.write_text('primary-started', encoding='utf-8')\n"
        "    time.sleep(90)\n"
        "print('multi-executor failover completed')\n"
    )
    try:
        primary = await _start_executor(
            port=primary_port,
            consumer_name=PRIMARY_CONSUMER,
            stream=stream,
            group=group,
        )
        await _wait_ready(primary_port)
        async with Client(f"http://127.0.0.1:{primary_port}/mcp") as client:
            submitted = await client.call_tool(
                "execution_submit",
                {
                    "request": {
                        "idempotency_key": f"multi-executor-submit-{unique}",
                        "mode": "STATIC",
                        "trigger_type": "INTERACTIVE",
                        "actor": {"type": "USER", "id": "multi-executor-user"},
                        "runtime_profile": "python3",
                        "source": inline_source(
                            f"multi-executor-plan-{unique}",
                            [{"tool_name": "multi_executor_failover", "code": code}],
                        ),
                        "context": {
                            "requested_by_user_id": "multi-executor-user",
                            "project_id": "multi-executor-project",
                            "session_id": f"multi-executor-session-{unique}",
                            "task_id": f"multi-executor-task-{unique}",
                        },
                    }
                },
            )
            if submitted.is_error:
                raise RuntimeError(str(submitted.content))
            execution_id = str(submitted.structured_content["execution_id"])
            running = await _wait_for(
                client,
                execution_id,
                {"RUNNING"},
                require_kernel=True,
            )
            initial_kernel = str(running["runtime_session_id"])
            first_attempts = await _attempts(client, execution_id)
            if len(first_attempts) != 1 or first_attempts[0]["lease_owner"] != PRIMARY_CONSUMER:
                raise RuntimeError(f"Primary did not own the first Attempt: {first_attempts}")

        secondary = await _start_executor(
            port=secondary_port,
            consumer_name=SECONDARY_CONSUMER,
            stream=stream,
            group=group,
        )
        await _wait_ready(secondary_port)
        primary.kill()
        await primary.wait()

        async with Client(f"http://127.0.0.1:{secondary_port}/mcp") as client:
            failed = await _wait_for_recovered_failure(client, execution_id)
            if (
                failed["failure_type"] != "LEASE_EXPIRED"
                or failed["retry_strategy"] != "FROM_START"
                or not failed["retryable"]
                or failed["runtime_session_cleanup_status"] != "SUCCEEDED"
                or failed["runtime_session_id"] is not None
            ):
                raise RuntimeError(f"Crash recovery was not classified safely: {failed}")

            retry = await client.call_tool(
                "execution_retry",
                {
                    "request": {
                        "execution_id": execution_id,
                        "idempotency_key": f"multi-executor-retry-{unique}",
                        "actor": {"type": "USER", "id": "multi-executor-user"},
                    }
                },
            )
            if retry.is_error or retry.structured_content["status"] != "QUEUED":
                raise RuntimeError(f"Failover retry was not queued: {retry.content}")
            succeeded = await _wait_for(
                client,
                execution_id,
                {"SUCCEEDED", "FAILED"},
            )
            attempts = await _attempts(client, execution_id)
            if (
                succeeded["status"] != "SUCCEEDED"
                or len(attempts) != 2
                or [item["lease_owner"] for item in attempts]
                != [PRIMARY_CONSUMER, SECONDARY_CONSUMER]
                or [item["status"] for item in attempts] != ["FAILED", "SUCCEEDED"]
                or attempts[0]["runtime_session_id"] == attempts[1]["runtime_session_id"]
            ):
                raise RuntimeError(
                    f"Secondary did not complete exactly one retry: {succeeded}, {attempts}"
                )

            print("execution_id:", execution_id)
            print("primary_exit_code:", primary.returncode)
            print("failure_type:", failed["failure_type"])
            print("retry_strategy:", failed["retry_strategy"])
            print("runtime_session_cleanup_status:", failed["runtime_session_cleanup_status"])
            print("initial_kernel:", initial_kernel)
            print("attempt_owners:", [item["lease_owner"] for item in attempts])
            print("attempt_statuses:", [item["status"] for item in attempts])
            print("final_status:", succeeded["status"])
    finally:
        await _stop_executor(primary)
        await _stop_executor(secondary)
        redis = Redis.from_url(
            os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
        )
        try:
            await redis.delete(stream, f"{stream}.dlq")
        finally:
            await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
