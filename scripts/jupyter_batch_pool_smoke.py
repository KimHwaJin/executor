"""Verify Worker and Jupyter capacity isolation across INTERACTIVE/BATCH pools."""

import asyncio
from typing import Any
from uuid import uuid4

from execution_spec_payload import execution_request, inline_spec
from local_test_support import local_runtime_specs
from mcp import Client


async def _execution(client: Client, execution_id: str) -> dict[str, Any]:
    result = await client.call_tool(
        "execution_get", {"execution_id": execution_id}
    )
    if result.is_error:
        raise RuntimeError(str(result.content))
    return result.structured_content


async def _wait_for_terminal(
    client: Client, execution_id: str
) -> dict[str, Any]:
    for _ in range(300):
        state = await _execution(client, execution_id)
        if state["state"]["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return state
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Execution {execution_id} did not finish in time.")


async def _wait_for_status(
    client: Client,
    execution_ids: list[str],
    expected_statuses: list[str],
) -> list[dict[str, Any]]:
    for _ in range(300):
        states = await asyncio.gather(
            *(
                _execution(client, execution_id)
                for execution_id in execution_ids
            )
        )
        if [state["state"]["status"] for state in states] == expected_statuses:
            return list(states)
        await asyncio.sleep(0.1)
    raise RuntimeError(
        f"Executions {execution_ids} did not reach statuses {expected_statuses}."
    )


async def _register_servers(
    client: Client, unique: str
) -> tuple[set[str], set[str]]:
    runtime_specs = {spec.name: spec for spec in local_runtime_specs()}
    interactive_spec = runtime_specs["local-jupyter"]
    interactive = await client.call_tool(
        "runtime_target_upsert",
        {
            "request": {
                "idempotency_key": f"batch-smoke-interactive-{unique}",
                "name": "local-jupyter",
                "runtime_type": "JUPYTER",
                "connection_config": {"endpoint": interactive_spec.endpoint},
                "credential": interactive_spec.token,
                "pool": "INTERACTIVE",
                "max_concurrent_executions": 1,
                "actor": {"type": "USER", "id": "batch-smoke-operator"},
            }
        },
    )
    batch_specs = (
        runtime_specs["local-jupyter-batch-primary"],
        runtime_specs["local-jupyter-batch-secondary"],
    )
    batch_results = []
    for spec in batch_specs:
        batch_results.append(
            await client.call_tool(
                "runtime_target_upsert",
                {
                    "request": {
                        "idempotency_key": f"batch-smoke-server-{spec.name}-{unique}",
                        "name": spec.name,
                        "runtime_type": "JUPYTER",
                        "connection_config": {"endpoint": spec.endpoint},
                        "credential": spec.token,
                        "pool": "BATCH",
                        "max_concurrent_executions": 1,
                        "actor": {
                            "type": "USER",
                            "id": "batch-smoke-operator",
                        },
                    }
                },
            )
        )
    results = [interactive, *batch_results]
    if any(
        result.is_error
        or result.structured_content["state"]["status"] != "ACTIVE"
        for result in results
    ):
        raise RuntimeError(
            f"Jupyter registration failed: {[item.content for item in results]}"
        )
    listed = await client.call_tool("runtime_target_list", {"limit": 200})
    if listed.is_error or listed.structured_content is None:
        raise RuntimeError(f"Runtime Target listing failed: {listed.content}")
    interactive_ids = {
        str(item["target_id"])
        for item in listed.structured_content["items"]
        if item["runtime"]["pool"] == "INTERACTIVE"
        and item["state"]["accepting_new_executions"]
    }
    return interactive_ids, {
        str(result.structured_content["target_id"]) for result in batch_results
    }


async def _submit(
    client: Client,
    *,
    unique: str,
    name: str,
    pool: str,
    sleep_seconds: int,
) -> str:
    result = await client.call_tool(
        "execution_submit",
        {
            "request": execution_request(
                idempotency_key=f"batch-pool-execution-{unique}-{name}",
                operation_mode="SINGLE",
                trigger_type="BATCH" if pool == "BATCH" else "INTERACTIVE",
                actor={
                    "type": "BATCH" if pool == "BATCH" else "USER",
                    "id": "batch-smoke-job"
                    if pool == "BATCH"
                    else "batch-pool-user",
                },
                runtime_profile="default",
                spec=inline_spec(
                    [
                        {
                            "skill_name": "report",
                            "tool_name": f"batch_pool_{name}",
                            "code": (
                                f"import time\ntime.sleep({sleep_seconds})\nprint('{name}')"
                            ),
                        }
                    ],
                ),
                context={
                    "user_id": "batch-pool-user",
                    "project_id": "batch-pool-project",
                    "session_id": f"batch-pool-session-{unique}-{name}",
                    "task_id": f"batch-pool-task-{unique}-{name}",
                    "workflow_id": (
                        f"batch-pool-workflow-{unique}-{name}"
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


async def main() -> None:
    unique = uuid4().hex
    async with Client("http://127.0.0.1:8000/mcp") as client:
        interactive_server_ids, batch_server_ids = await _register_servers(
            client, unique
        )
        batch_ids = [
            await _submit(
                client,
                unique=unique,
                name=f"batch-{index}",
                pool="BATCH",
                sleep_seconds=6,
            )
            for index in range(2)
        ]
        await _wait_for_status(client, batch_ids, ["RUNNING", "RUNNING"])

        interactive_id = await _submit(
            client,
            unique=unique,
            name="interactive",
            pool="INTERACTIVE",
            sleep_seconds=0,
        )
        interactive_state = await _wait_for_terminal(client, interactive_id)
        running_batch_states = await asyncio.gather(
            *(_execution(client, execution_id) for execution_id in batch_ids)
        )
        if interactive_state["state"]["status"] != "SUCCEEDED" or any(
            state["state"]["status"] != "RUNNING"
            for state in running_batch_states
        ):
            raise RuntimeError(
                "INTERACTIVE work did not finish independently while BATCH Worker slots "
                f"were saturated: interactive={interactive_state}, "
                f"batch={running_batch_states}"
            )

        queued_batch_id = await _submit(
            client,
            unique=unique,
            name="batch-queued",
            pool="BATCH",
            sleep_seconds=1,
        )
        batch_ids.append(queued_batch_id)

        observed_batch_queue = False
        for _ in range(80):
            states = await asyncio.gather(
                *(
                    _execution(client, execution_id)
                    for execution_id in batch_ids
                )
            )
            statuses = [state["state"]["status"] for state in states]
            if (
                statuses.count("RUNNING") == 2
                and statuses.count("QUEUED") == 1
            ):
                observed_batch_queue = True
                break
            await asyncio.sleep(0.1)
        if not observed_batch_queue:
            raise RuntimeError(
                "Did not observe two running and one queued BATCH execution."
            )

        batch_states = await asyncio.gather(
            *(
                _wait_for_terminal(client, execution_id)
                for execution_id in batch_ids
            ),
        )
        if any(
            state["state"]["status"] != "SUCCEEDED" for state in batch_states
        ):
            raise RuntimeError(f"BATCH pool execution failed: {batch_states}")
        actual_batch_servers = {
            str(state["runtime"]["target_id"]) for state in batch_states
        }
        if actual_batch_servers != batch_server_ids:
            raise RuntimeError(
                f"Expected both BATCH servers {batch_server_ids}, got {actual_batch_servers}"
            )
        actual_interactive_server = str(
            interactive_state["runtime"]["target_id"]
        )
        if actual_interactive_server not in interactive_server_ids:
            raise RuntimeError(
                "INTERACTIVE execution escaped its configured pool."
            )
        if actual_interactive_server in actual_batch_servers:
            raise RuntimeError(
                "INTERACTIVE and BATCH pools shared a server unexpectedly."
            )

        print("interactive_status:", interactive_state["state"]["status"])
        print("interactive_completed_while_batch_saturated:", True)
        print(
            "batch_statuses:",
            [state["state"]["status"] for state in batch_states],
        )
        print("batch_queue_observed:", observed_batch_queue)
        print("distinct_batch_servers:", len(actual_batch_servers))
        print("interactive_server_id:", actual_interactive_server)
        print("batch_server_ids:", sorted(actual_batch_servers))


if __name__ == "__main__":
    asyncio.run(main())
