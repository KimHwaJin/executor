"""Verify notebook reads fail over across Jupyter targets sharing one storage volume."""

import asyncio
import os
from typing import Any
from uuid import uuid4

from execution_spec_payload import execution_request, inline_spec
from mcp import Client


async def _required(
    client: Client, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    if result.is_error or result.structured_content is None:
        raise RuntimeError(f"{tool} failed: {result.content}")
    return result.structured_content


async def _wait_terminal(client: Client, execution_id: str) -> dict[str, Any]:
    for _ in range(300):
        execution = await _required(
            client, "execution_get", {"execution_id": execution_id}
        )
        if execution["state"]["status"] in {
            "SUCCEEDED",
            "FAILED",
            "CANCELLED",
        }:
            return execution
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Execution {execution_id} did not finish.")


async def main() -> None:
    unique = uuid4().hex
    mcp_url = os.getenv("EXECUTOR_MCP_URL", "http://127.0.0.1:8000/mcp")
    primary_endpoint = os.getenv(
        "SHARED_STORAGE_PRIMARY_ENDPOINT", "http://jupyter:8888"
    )
    secondary_endpoint = os.getenv(
        "SHARED_STORAGE_SECONDARY_ENDPOINT", "http://jupyter-secondary:8888"
    )
    secondary_token = os.getenv(
        "JUPYTER_SECONDARY_TOKEN", "change-me-secondary-local-only"
    )
    operator = {"type": "USER", "id": "shared-storage-operator"}

    async with Client(mcp_url) as client:
        targets = []
        for name, endpoint, credential in (
            ("local-jupyter", primary_endpoint, None),
            ("local-jupyter-secondary", secondary_endpoint, secondary_token),
        ):
            request: dict[str, Any] = {
                "idempotency_key": f"shared-storage-upsert-{name}-{unique}",
                "name": name,
                "runtime_type": "JUPYTER",
                "connection_config": {"endpoint": endpoint},
                "pool": "INTERACTIVE",
                "max_concurrent_executions": 1,
                "actor": operator,
            }
            if credential is not None:
                request["credential"] = credential
            target = await _required(
                client, "runtime_target_upsert", {"request": request}
            )
            if target["state"]["status"] != "ACTIVE":
                raise RuntimeError(f"Runtime Target is not ACTIVE: {target}")
            targets.append(target)

        submitted = await _required(
            client,
            "execution_submit",
            {
                "request": execution_request(
                    idempotency_key=f"shared-storage-execution-{unique}",
                    operation_mode="SINGLE",
                    trigger_type="INTERACTIVE",
                    runtime_profile="basic",
                    spec=inline_spec(
                        [
                            {
                                "skill_name": "report",
                                "tool_name": "shared_storage_probe",
                                "code": "shared_storage_value = 42\nprint(shared_storage_value)",
                            }
                        ],
                    ),
                    context={
                        "user_id": "shared-storage-user",
                        "project_id": "shared-storage-project",
                        "session_id": f"shared-storage-session-{unique}",
                        "task_id": f"shared-storage-task-{unique}",
                    },
                    actor={"type": "USER", "id": "shared-storage-user"},
                )
            },
        )
        execution_id = str(submitted["execution_id"])
        execution = await _wait_terminal(client, execution_id)
        if execution["state"]["status"] != "SUCCEEDED":
            raise RuntimeError(f"Execution failed: {execution}")

        historical_target_id = str(execution["runtime"]["target_id"])
        await _required(
            client,
            "runtime_target_disable",
            {
                "request": {
                    "target_id": historical_target_id,
                    "idempotency_key": f"shared-storage-disable-{unique}",
                    "actor": operator,
                }
            },
        )
        try:
            notebook = await _required(
                client,
                "execution_notebook_read",
                {
                    "execution_id": execution_id,
                    "view": "FULL",
                    "limit": 200,
                },
            )
            if notebook["page"]["total_count"] != 1 or "42" not in str(
                notebook["cells"]
            ):
                raise RuntimeError(
                    f"Fallback notebook content is invalid: {notebook}"
                )
        finally:
            await _required(
                client,
                "runtime_target_set_state",
                {
                    "request": {
                        "target_id": historical_target_id,
                        "desired_state": "ACTIVE",
                        "idempotency_key": f"shared-storage-reactivate-{unique}",
                        "actor": operator,
                    }
                },
            )

    print("execution_id:", execution_id)
    print("historical_target_id:", historical_target_id)
    print("fallback_target_count:", len(targets) - 1)
    print("notebook_cell_count:", notebook["page"]["total_count"])


if __name__ == "__main__":
    asyncio.run(main())
