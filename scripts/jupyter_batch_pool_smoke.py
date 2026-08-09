"""Verify INTERACTIVE isolation, two BATCH servers, and queued capacity recovery."""

import asyncio
import os
from typing import Any
from uuid import uuid4

from mcp import Client


async def _execution(client: Client, execution_id: str) -> dict[str, Any]:
    result = await client.call_tool("execution_get", {"execution_id": execution_id})
    if result.is_error:
        raise RuntimeError(str(result.content))
    return result.structured_content


async def _wait_for_terminal(client: Client, execution_id: str) -> dict[str, Any]:
    for _ in range(300):
        state = await _execution(client, execution_id)
        if state["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return state
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Execution {execution_id} did not finish in time.")


async def _register_servers(client: Client, unique: str) -> tuple[str, set[str]]:
    interactive = await client.call_tool(
        "jupyter_server_upsert",
        {
            "request": {
                "idempotency_key": f"batch-smoke-interactive-{unique}",
                "name": "local-jupyter",
                "endpoint": "http://127.0.0.1:8888",
                "pool": "INTERACTIVE",
                "max_concurrent_executions": 1,
            }
        },
    )
    batch_specs = (
        (
            "local-jupyter-batch-primary",
            "http://127.0.0.1:8890",
            os.getenv(
                "JUPYTER_BATCH_PRIMARY_TOKEN",
                "change-me-batch-primary-local-only",
            ),
        ),
        (
            "local-jupyter-batch-secondary",
            "http://127.0.0.1:8891",
            os.getenv(
                "JUPYTER_BATCH_SECONDARY_TOKEN",
                "change-me-batch-secondary-local-only",
            ),
        ),
    )
    batch_results = []
    for name, endpoint, token in batch_specs:
        batch_results.append(
            await client.call_tool(
                "jupyter_server_upsert",
                {
                    "request": {
                        "idempotency_key": f"batch-smoke-server-{name}-{unique}",
                        "name": name,
                        "endpoint": endpoint,
                        "token": token,
                        "pool": "BATCH",
                        "max_concurrent_executions": 1,
                    }
                },
            )
        )
    results = [interactive, *batch_results]
    if any(
        result.is_error or result.structured_content["status"] != "ACTIVE"
        for result in results
    ):
        raise RuntimeError(f"Jupyter registration failed: {[item.content for item in results]}")
    return (
        str(interactive.structured_content["server_id"]),
        {str(result.structured_content["server_id"]) for result in batch_results},
    )


async def _submit(
    client: Client,
    *,
    unique: str,
    name: str,
    pool: str,
) -> str:
    result = await client.call_tool(
        "execution_submit",
        {
            "request": {
                "idempotency_key": f"batch-pool-execution-{unique}-{name}",
                "mode": "STATIC",
                "trigger_type": "BATCH" if pool == "BATCH" else "INTERACTIVE",
                "jupyter_pool": pool,
                "kernel_name": "python3",
                "source": {
                    "type": "INLINE",
                    "code": f"import time\ntime.sleep(4)\nprint('{name}')\n",
                },
                "context": {
                    "requested_by_user_id": "batch-pool-user",
                    "project_id": "batch-pool-project",
                    "session_id": f"batch-pool-session-{unique}-{name}",
                    "execution_plan_id": f"batch-pool-plan-{unique}-{name}",
                },
                "steps": [
                    {
                        "sequence": 0,
                        "skill_name": "report",
                        "tool_name": f"batch_pool_{name}",
                    }
                ],
            }
        },
    )
    if result.is_error:
        raise RuntimeError(str(result.content))
    return str(result.structured_content["execution_id"])


async def main() -> None:
    unique = uuid4().hex
    async with Client("http://127.0.0.1:8000/mcp") as client:
        interactive_server_id, batch_server_ids = await _register_servers(client, unique)
        interactive_id = await _submit(
            client,
            unique=unique,
            name="interactive",
            pool="INTERACTIVE",
        )
        batch_ids = [
            await _submit(
                client,
                unique=unique,
                name=f"batch-{index}",
                pool="BATCH",
            )
            for index in range(3)
        ]

        observed_batch_queue = False
        for _ in range(80):
            states = await asyncio.gather(
                *(_execution(client, execution_id) for execution_id in batch_ids)
            )
            statuses = [state["status"] for state in states]
            if statuses.count("RUNNING") == 2 and statuses.count("QUEUED") == 1:
                observed_batch_queue = True
                break
            await asyncio.sleep(0.1)
        if not observed_batch_queue:
            raise RuntimeError("Did not observe two running and one queued BATCH execution.")

        interactive_state, *batch_states = await asyncio.gather(
            _wait_for_terminal(client, interactive_id),
            *(_wait_for_terminal(client, execution_id) for execution_id in batch_ids),
        )
        if interactive_state["status"] != "SUCCEEDED" or any(
            state["status"] != "SUCCEEDED" for state in batch_states
        ):
            raise RuntimeError(
                f"Pool execution failed: interactive={interactive_state}, batch={batch_states}"
            )
        actual_batch_servers = {
            str(state["jupyter_server_id"]) for state in batch_states
        }
        if actual_batch_servers != batch_server_ids:
            raise RuntimeError(
                f"Expected both BATCH servers {batch_server_ids}, got {actual_batch_servers}"
            )
        if str(interactive_state["jupyter_server_id"]) != interactive_server_id:
            raise RuntimeError("INTERACTIVE execution escaped its configured pool server.")
        if interactive_server_id in actual_batch_servers:
            raise RuntimeError("INTERACTIVE and BATCH pools shared a server unexpectedly.")

        print("interactive_status:", interactive_state["status"])
        print("batch_statuses:", [state["status"] for state in batch_states])
        print("batch_queue_observed:", observed_batch_queue)
        print("distinct_batch_servers:", len(actual_batch_servers))
        print("interactive_server_id:", interactive_server_id)
        print("batch_server_ids:", sorted(actual_batch_servers))


if __name__ == "__main__":
    asyncio.run(main())
