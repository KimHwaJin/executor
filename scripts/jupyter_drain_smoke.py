"""Verify drain protects in-flight work and blocks new scheduling."""

import asyncio
import os
from typing import Any
from uuid import uuid4

from execution_spec_payload import inline_source
from mcp import Client


async def _state(client: Client, execution_id: str) -> dict[str, Any]:
    result = await client.call_tool("execution_get", {"execution_id": execution_id})
    return result.structured_content


async def _wait_status(client: Client, execution_id: str, statuses: set[str]) -> dict[str, Any]:
    for _ in range(200):
        state = await _state(client, execution_id)
        if state["status"] in statuses:
            return state
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Execution {execution_id} did not reach {statuses}.")


async def _set_state(client: Client, server_id: str, state: str, unique: str) -> dict[str, Any]:
    result = await client.call_tool(
        "runtime_target_set_state",
        {
            "request": {
                "idempotency_key": (f"drain-{server_id}-{state}-{unique}-{uuid4()}"),
                "target_id": server_id,
                "desired_state": state,
                "actor": {"type": "USER", "id": "drain-operator"},
            }
        },
    )
    if result.is_error:
        raise RuntimeError(str(result.content))
    return result.structured_content


async def _submit(client: Client, unique: str, index: int, sleep: int) -> str:
    result = await client.call_tool(
        "execution_submit",
        {
            "request": {
                "idempotency_key": f"drain-execution-{unique}-{index}",
                "mode": "STATIC",
                "trigger_type": "INTERACTIVE",
                "actor": {"type": "USER", "id": "drain-user"},
                "runtime_profile": "python3",
                "source": inline_source(
                    f"drain-plan-{unique}-{index}",
                    [
                        {
                            "tool_name": "drain_test",
                            "code": f"import time\ntime.sleep({sleep})\nprint('done {index}')",
                        }
                    ],
                ),
                "context": {
                    "requested_by_user_id": "drain-user",
                    "project_id": "drain-project",
                    "session_id": f"drain-session-{index}",
                    "task_id": f"drain-task-{unique}-{index}",
                },
            }
        },
    )
    return result.structured_content["execution_id"]


async def main() -> None:
    unique = str(uuid4())
    secondary_token = os.environ.get("JUPYTER_SECONDARY_TOKEN", "change-me-secondary-local-only")
    async with Client("http://127.0.0.1:8000/mcp") as client:
        listed = await client.call_tool("runtime_target_list", {})
        listed_payload = listed.structured_content
        listed_items = listed_payload.get("items", [])
        servers = {item["name"]: item for item in listed_items}
        primary = servers["local-jupyter"]
        secondary = servers.get("local-jupyter-secondary")
        if secondary is None:
            registered = await client.call_tool(
                "runtime_target_upsert",
                {
                    "request": {
                        "idempotency_key": f"drain-register-{unique}",
                        "name": "local-jupyter-secondary",
                        "runtime_type": "JUPYTER",
                        "connection_config": {"endpoint": "http://127.0.0.1:8889"},
                        "credential": secondary_token,
                        "pool": "INTERACTIVE",
                        "max_concurrent_executions": 1,
                        "actor": {"type": "USER", "id": "drain-operator"},
                    }
                },
            )
            secondary = registered.structured_content

        await _set_state(client, primary["target_id"], "ACTIVE", unique)
        await _set_state(client, secondary["target_id"], "DRAINING", unique)
        first_id = await _submit(client, unique, 1, 3)
        running = await _wait_status(client, first_id, {"RUNNING"})
        if running["runtime_target_id"] != primary["target_id"]:
            raise RuntimeError("First execution was not scheduled on the only active server.")

        draining = await _set_state(client, primary["target_id"], "DRAINING", unique)
        if draining["drain_complete"] or draining["active_execution_count"] != 1:
            raise RuntimeError(f"In-flight execution was not reported during drain: {draining}")

        second_id = await _submit(client, unique, 2, 0)
        await asyncio.sleep(1)
        if (await _state(client, second_id))["status"] != "QUEUED":
            raise RuntimeError("New work should remain queued while every server is draining.")

        await _wait_status(client, first_id, {"SUCCEEDED"})
        drained = await client.call_tool("runtime_target_get", {"target_id": primary["target_id"]})
        if not drained.structured_content["drain_complete"]:
            raise RuntimeError("Drain did not complete after in-flight work finished.")

        await _set_state(client, secondary["target_id"], "ACTIVE", unique)
        second = await _wait_status(client, second_id, {"SUCCEEDED", "FAILED"})
        if second["status"] != "SUCCEEDED" or second["runtime_target_id"] != secondary["target_id"]:
            raise RuntimeError(f"Queued work did not move to reactivated server: {second}")
        await _set_state(client, primary["target_id"], "ACTIVE", unique)

        print("in_flight_completed:", True)
        print("new_work_waited_while_draining:", True)
        print("queued_work_reassigned:", True)


if __name__ == "__main__":
    asyncio.run(main())
