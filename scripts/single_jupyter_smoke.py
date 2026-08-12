"""Register one Jupyter server and verify an end-to-end STATIC execution."""

import asyncio
import os
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from execution_spec_payload import inline_source
from mcp import Client

from executor_service.config import get_settings


async def _required_tool_result(
    client: Client, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    if result.is_error or result.structured_content is None:
        raise RuntimeError(f"{tool} failed: {result.content}")
    return result.structured_content


async def _wait_for_terminal(
    client: Client, execution_id: str, timeout_seconds: float
) -> dict[str, Any]:
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        execution = await _required_tool_result(
            client, "execution_get", {"execution_id": execution_id}
        )
        if execution["state"]["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return execution
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Execution {execution_id} did not finish within {timeout_seconds} seconds.")


async def main() -> None:
    settings = get_settings()
    mcp_url = os.getenv("EXECUTOR_MCP_URL", "http://127.0.0.1:8000/mcp")
    server_name = os.getenv("SINGLE_JUPYTER_NAME", settings.runtime_target_name)
    endpoint = os.getenv("SINGLE_JUPYTER_ENDPOINT", settings.jupyter_endpoint)
    token = os.getenv("SINGLE_JUPYTER_TOKEN", settings.jupyter_auth_token)
    kernel_name = os.getenv("SINGLE_JUPYTER_KERNEL", "basic")
    timeout_seconds = float(os.getenv("SINGLE_JUPYTER_TIMEOUT_SECONDS", "120"))
    unique = uuid4().hex

    async with Client(mcp_url) as client:
        server = await _required_tool_result(
            client,
            "runtime_target_upsert",
            {
                "request": {
                    "idempotency_key": f"single-jupyter-register-{unique}",
                    "name": server_name,
                    "runtime_type": "JUPYTER",
                    "connection_config": {"endpoint": endpoint},
                    "credential": token,
                    "pool": "INTERACTIVE",
                    "max_concurrent_executions": 1,
                    "actor": {"type": "USER", "id": "single-jupyter-operator"},
                }
            },
        )
        if server["state"]["status"] != "ACTIVE":
            raise RuntimeError(f"Jupyter server is not ACTIVE: {server.get('last_health_error')}")
        if kernel_name not in server["runtime"]["supported_profiles"]:
            raise RuntimeError(
                f"Kernel {kernel_name!r} is unavailable: {server['supported_profiles']}"
            )

        submitted = await _required_tool_result(
            client,
            "execution_submit",
            {
                "request": {
                    "idempotency_key": f"single-jupyter-submit-{unique}",
                    "mode": "STATIC",
                    "trigger_type": "INTERACTIVE",
                    "actor": {"type": "USER", "id": "single-jupyter-user"},
                    "runtime_profile": kernel_name,
                    "source": inline_source(
                        f"single-jupyter-plan-{unique}",
                        [
                            {
                                "skill_name": "eda",
                                "tool_name": "sum_values",
                                "code": "values = [1, 2, 3]\ntotal = sum(values)\nprint(total)",
                            },
                            {
                                "skill_name": "report",
                                "tool_name": "write_smoke_result",
                                "code": (
                                    "from pathlib import Path\n"
                                    "assert total == 6\n"
                                    "Path('artifacts/other/single-jupyter-smoke.txt').write_text("
                                    "str(total), encoding='utf-8')\n"
                                    "print('artifact written')"
                                ),
                            },
                        ],
                    ),
                    "context": {
                        "user_id": "single-jupyter-user",
                        "project_id": "single-jupyter-project",
                        "session_id": f"single-jupyter-session-{unique}",
                        "task_id": f"single-jupyter-task-{unique}",
                    },
                }
            },
        )
        execution_id = str(submitted["execution_id"])
        terminal = await _wait_for_terminal(client, execution_id, timeout_seconds)
        if terminal["state"]["status"] != "SUCCEEDED":
            raise RuntimeError(f"Execution did not succeed: {terminal}")
        if str(terminal["runtime"]["target_id"]) != str(server["target_id"]):
            raise RuntimeError("Execution used a different Jupyter server.")

        artifacts_page = await _required_tool_result(
            client,
            "execution_artifact_list",
            {"execution_id": execution_id, "limit": 100},
        )
        artifact_names = {item["name"] for item in artifacts_page["items"]}
        required_artifacts = {"single-jupyter-smoke.txt", "execution.ipynb"}
        if not required_artifacts.issubset(artifact_names):
            raise RuntimeError(f"Expected Artifacts were not registered: {artifact_names}")

    notebook_path = terminal.get("notebook_path")
    if not notebook_path:
        raise RuntimeError("Execution did not return a notebook_path.")
    notebook = settings.workspace_host_root / Path(notebook_path)
    result_file = notebook.parents[1] / "artifacts" / "other" / "single-jupyter-smoke.txt"
    if not notebook.is_file() or result_file.read_text(encoding="utf-8") != "6":
        raise RuntimeError("Expected local Notebook or smoke Artifact was not created.")

    print("runtime_target_id:", server["target_id"])
    print("execution_id:", execution_id)
    print("status:", terminal["state"]["status"])
    print("step_statuses:", [step["result"]["status"] for step in terminal["steps"]])
    print("notebook:", notebook)
    print("artifacts:", sorted(artifact_names))


if __name__ == "__main__":
    asyncio.run(main())
