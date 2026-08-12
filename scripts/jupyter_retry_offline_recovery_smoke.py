"""Verify an OFFLINE retained-kernel retry waits and resumes after server recovery."""

import asyncio
import os
from typing import Any
from uuid import uuid4

from execution_spec_payload import inline_source
from mcp import Client


async def _required(
    client: Client, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    if result.is_error or result.structured_content is None:
        raise RuntimeError(f"{tool} failed: {result.content}")
    return result.structured_content


async def _wait_for_status(
    client: Client,
    execution_id: str,
    statuses: set[str],
    *,
    timeout_seconds: float = 60,
) -> dict[str, Any]:
    for _ in range(int(timeout_seconds * 4)):
        execution = await _required(
            client, "execution_get", {"execution_id": execution_id}
        )
        if execution["status"] in statuses:
            return execution
        await asyncio.sleep(0.25)
    raise RuntimeError(f"Execution {execution_id} did not reach {statuses}.")


async def _upsert_server(
    client: Client,
    *,
    unique: str,
    suffix: str,
    name: str,
    endpoint: str,
    token: str,
) -> dict[str, Any]:
    return await _required(
        client,
        "jupyter_server_upsert",
        {
            "request": {
                "idempotency_key": f"retry-offline-server-{suffix}-{unique}",
                "name": name,
                "endpoint": endpoint,
                "token": token,
                "pool": "INTERACTIVE",
                "max_concurrent_executions": 2,
                "actor": {"type": "USER", "id": "retry-offline-operator"},
            }
        },
    )


async def main() -> None:
    unique = uuid4().hex
    mcp_url = os.getenv("EXECUTOR_MCP_URL", "http://127.0.0.1:8000/mcp")
    server_name = os.getenv("JUPYTER_SERVER_NAME", "local-jupyter")
    healthy_endpoint = os.getenv(
        "RETRY_RECOVERY_JUPYTER_ENDPOINT", "http://jupyter:8888"
    )
    offline_endpoint = os.getenv(
        "RETRY_RECOVERY_OFFLINE_ENDPOINT", "http://127.0.0.1:9"
    )
    token = os.getenv("JUPYTER_TOKEN", "change-me-local-only")
    server_was_redirected = False

    async with Client(mcp_url) as client:
        healthy_server = await _upsert_server(
            client,
            unique=unique,
            suffix="initial",
            name=server_name,
            endpoint=healthy_endpoint,
            token=token,
        )
        if healthy_server["status"] != "ACTIVE":
            raise RuntimeError(f"Jupyter server was not initially ACTIVE: {healthy_server}")

        submitted = await _required(
            client,
            "execution_submit",
            {
                "request": {
                    "idempotency_key": f"retry-offline-submit-{unique}",
                    "mode": "STATIC",
                    "trigger_type": "INTERACTIVE",
                    "actor": {"type": "USER", "id": "retry-offline-user"},
                    "kernel_name": "python3",
                    "source": inline_source(
                        f"retry-offline-plan-{unique}",
                        [
                            {
                                "tool_name": "initialize",
                                "code": "attempt_counter = 0",
                            },
                            {
                                "tool_name": "fail_once",
                                "code": (
                                    "attempt_counter += 1\n"
                                    "if attempt_counter == 1:\n"
                                    "    raise RuntimeError('expected first failure')\n"
                                    "print(attempt_counter)"
                                ),
                            },
                            {"tool_name": "finish", "code": "print('recovered')"},
                        ],
                    ),
                    "context": {
                        "requested_by_user_id": "retry-offline-user",
                        "project_id": "retry-offline-project",
                        "session_id": f"retry-offline-session-{unique}",
                        "task_id": f"retry-offline-task-{unique}",
                    },
                }
            },
        )
        execution_id = str(submitted["execution_id"])
        failed = await _wait_for_status(client, execution_id, {"FAILED"})
        if failed["retry_strategy"] != "FROM_FAILED_STEP":
            raise RuntimeError(f"Execution failure did not retain its kernel: {failed}")
        original_server_id = str(failed["jupyter_server_id"])
        original_kernel_id = str(failed["kernel_id"])

        try:
            offline_server = await _upsert_server(
                client,
                unique=unique,
                suffix="offline",
                name=server_name,
                endpoint=offline_endpoint,
                token=token,
            )
            server_was_redirected = True
            if offline_server["status"] != "OFFLINE":
                raise RuntimeError(f"Jupyter server did not become OFFLINE: {offline_server}")

            await _required(
                client,
                "execution_retry",
                {
                    "request": {
                        "execution_id": execution_id,
                        "idempotency_key": f"retry-offline-command-{unique}",
                        "actor": {"type": "USER", "id": "retry-offline-user"},
                    }
                },
            )
            await asyncio.sleep(3)
            waiting = await _required(
                client, "execution_get", {"execution_id": execution_id}
            )
            if (
                waiting["status"] != "QUEUED"
                or waiting["retry_strategy"] != "FROM_FAILED_STEP"
                or str(waiting["jupyter_server_id"]) != original_server_id
                or str(waiting["kernel_id"]) != original_kernel_id
            ):
                raise RuntimeError(
                    f"OFFLINE retry did not remain pinned to its retained kernel: {waiting}"
                )

            recovered_server = await _upsert_server(
                client,
                unique=unique,
                suffix="recovered",
                name=server_name,
                endpoint=healthy_endpoint,
                token=token,
            )
            server_was_redirected = False
            if recovered_server["status"] != "ACTIVE":
                raise RuntimeError(f"Jupyter server did not recover: {recovered_server}")

            succeeded = await _wait_for_status(
                client, execution_id, {"SUCCEEDED", "FAILED"}
            )
            trace = await _required(
                client, "execution_trace_get", {"execution_id": execution_id}
            )
            attempts = trace["attempts"]["items"]
            retry_attempt = attempts[-1] if attempts else None
            if (
                succeeded["status"] != "SUCCEEDED"
                or str(succeeded["jupyter_server_id"]) != original_server_id
                or retry_attempt is None
                or str(retry_attempt["jupyter_server_id"]) != original_server_id
                or str(retry_attempt["kernel_id"]) != original_kernel_id
            ):
                raise RuntimeError(f"Retained-kernel recovery failed: {succeeded}")

            print("execution_id:", execution_id)
            print("offline_wait_status:", waiting["status"])
            print("same_server:", str(succeeded["jupyter_server_id"]) == original_server_id)
            print("same_kernel:", str(retry_attempt["kernel_id"]) == original_kernel_id)
            print("final_status:", succeeded["status"])
        finally:
            if server_was_redirected:
                await _upsert_server(
                    client,
                    unique=unique,
                    suffix="finally-recovered",
                    name=server_name,
                    endpoint=healthy_endpoint,
                    token=token,
                )


if __name__ == "__main__":
    asyncio.run(main())
