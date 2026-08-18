"""Submit a SINGLE execution through MCP and wait for its Jupyter result."""

import asyncio
from uuid import uuid4

from execution_spec_payload import execution_request, inline_source
from mcp import Client


async def main() -> None:
    unique = str(uuid4())
    async with Client("http://127.0.0.1:8000/mcp") as client:
        submitted = await client.call_tool(
            "execution_submit",
            {
                "request": execution_request(
                    idempotency_key=f"jupyter-smoke-{unique}",
                    operation_mode="SINGLE",
                    trigger_type="INTERACTIVE",
                    actor={"type": "USER", "id": "smoke-user"},
                    runtime_profile="basic",
                    source=inline_source(
                        [
                            {
                                "skill_name": "eda",
                                "tool_name": "sum_values",
                                "code": "numbers = [1, 2, 3]\nprint(sum(numbers))",
                            },
                            {
                                "skill_name": "report",
                                "tool_name": "save_result",
                                "code": (
                                    "from pathlib import Path\n"
                                    "Path('artifacts/other/result.txt').write_text("
                                    "'completed', encoding='utf-8')\n"
                                    "len(numbers)"
                                ),
                            },
                        ],
                    ),
                    context={
                        "user_id": "smoke-user",
                        "project_id": "smoke-project",
                        "session_id": "smoke-session",
                        "task_id": f"smoke-task-{unique}",
                    },
                )
            },
        )
        if submitted.is_error:
            raise RuntimeError(str(submitted.content))
        execution_id = submitted.structured_content["execution_id"]
        terminal = None
        for _ in range(150):
            result = await client.call_tool("execution_get", {"execution_id": execution_id})
            terminal = result.structured_content
            if terminal["state"]["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                break
            await asyncio.sleep(0.2)
        if terminal is None or terminal["state"]["status"] != "SUCCEEDED":
            raise RuntimeError(f"Execution did not succeed: {terminal}")
        steps_result = await client.call_tool(
            "execution_step_list", {"execution_id": execution_id, "limit": 100}
        )
        if steps_result.is_error:
            raise RuntimeError(str(steps_result.content))
        steps = steps_result.structured_content["items"]

        notebook = await client.call_tool(
            "execution_notebook_read",
            {"execution_id": execution_id, "response_format": "detailed", "limit": 0},
        )
        if notebook.is_error or len(notebook.structured_content["cells"]) != 2:
            raise RuntimeError("Expected Runtime-owned Notebook was not readable.")
        artifacts = await client.call_tool(
            "execution_artifact_list", {"execution_id": execution_id, "limit": 100}
        )
        if not {"result.txt", "execution.ipynb"}.issubset(
            {item["name"] for item in artifacts.structured_content["items"]}
        ):
            raise RuntimeError("Expected Runtime-owned Artifact metadata was not registered.")
        print("execution_id:", execution_id)
        print("status:", terminal["state"]["status"])
        print("notebook_path:", terminal["workspace"]["notebook_path"])
        print("step_statuses:", [step["result"]["status"] for step in steps])


if __name__ == "__main__":
    asyncio.run(main())
