"""Register two Jupyter servers and verify concurrent executions are distributed."""

import asyncio
import os
from typing import Any
from uuid import uuid4

from execution_spec_payload import execution_request, inline_source
from mcp import Client


async def _wait_for_terminal(client: Client, execution_id: str) -> dict[str, Any]:
    for _ in range(200):
        result = await client.call_tool("execution_get", {"execution_id": execution_id})
        state = result.structured_content
        if state["state"]["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return state
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Execution {execution_id} did not finish in time.")


async def main() -> None:
    unique = str(uuid4())
    secondary_token = os.environ.get("JUPYTER_SECONDARY_TOKEN", "change-me-secondary-local-only")
    async with Client("http://127.0.0.1:8000/mcp") as client:
        primary = await client.call_tool(
            "runtime_target_upsert",
            {
                "request": {
                    "idempotency_key": f"fleet-primary-{unique}",
                    "name": "local-jupyter",
                    "runtime_type": "JUPYTER",
                    "connection_config": {"endpoint": "http://127.0.0.1:8888"},
                    "pool": "INTERACTIVE",
                    "max_concurrent_executions": 1,
                    "actor": {"type": "USER", "id": "fleet-operator"},
                }
            },
        )
        secondary = await client.call_tool(
            "runtime_target_upsert",
            {
                "request": {
                    "idempotency_key": f"fleet-secondary-{unique}",
                    "name": "local-jupyter-secondary",
                    "runtime_type": "JUPYTER",
                    "connection_config": {"endpoint": "http://127.0.0.1:8889"},
                    "credential": secondary_token,
                    "pool": "INTERACTIVE",
                    "max_concurrent_executions": 1,
                    "actor": {"type": "USER", "id": "fleet-operator"},
                }
            },
        )
        for result in (primary, secondary):
            if result.is_error or result.structured_content["state"]["status"] != "ACTIVE":
                raise RuntimeError(f"Jupyter registration failed: {result.content}")

        execution_ids: list[str] = []
        for index in range(2):
            submitted = await client.call_tool(
                "execution_submit",
                {
                    "request": execution_request(
                        idempotency_key=f"fleet-execution-{unique}-{index}",
                        operation_mode="SINGLE",
                        trigger_type="INTERACTIVE",
                        actor={"type": "USER", "id": "fleet-user"},
                        runtime_profile="basic",
                        source=inline_source(
                            [
                                {
                                    "tool_name": "fleet_test",
                                    "code": (
                                        "import time\ntime.sleep(3)\n"
                                        f"print('fleet execution {index}')"
                                    ),
                                }
                            ],
                        ),
                        context={
                            "user_id": "fleet-user",
                            "project_id": "fleet-project",
                            "session_id": f"fleet-session-{index}",
                            "task_id": f"fleet-task-{unique}-{index}",
                        },
                    )
                },
            )
            if submitted.is_error:
                raise RuntimeError(str(submitted.content))
            execution_ids.append(submitted.structured_content["execution_id"])

        states = await asyncio.gather(
            *(_wait_for_terminal(client, execution_id) for execution_id in execution_ids)
        )
        server_ids = {str(state["runtime"]["target_id"]) for state in states}
        if any(state["state"]["status"] != "SUCCEEDED" for state in states) or len(server_ids) != 2:
            raise RuntimeError(f"Executions were not distributed successfully: {states}")

        print("statuses:", [state["state"]["status"] for state in states])
        print("distinct_runtime_targets:", len(server_ids))
        print("execution_ids:", execution_ids)


if __name__ == "__main__":
    asyncio.run(main())
