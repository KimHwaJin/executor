"""Submit a STATIC execution through MCP and wait for its Jupyter result."""

import asyncio
from pathlib import Path
from uuid import uuid4

from execution_spec_payload import inline_source
from mcp import Client

from executor_service.config import get_settings


async def main() -> None:
    unique = str(uuid4())
    async with Client("http://127.0.0.1:8000/mcp") as client:
        submitted = await client.call_tool(
            "execution_submit",
            {
                "request": {
                    "idempotency_key": f"jupyter-smoke-{unique}",
                    "mode": "STATIC",
                    "trigger_type": "INTERACTIVE",
                    "actor": {"type": "USER", "id": "smoke-user"},
                    "runtime_profile": "basic",
                    "source": inline_source(
                        f"smoke-plan-{unique}",
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
                    "context": {
                        "user_id": "smoke-user",
                        "project_id": "smoke-project",
                        "session_id": "smoke-session",
                        "task_id": f"smoke-task-{unique}",
                    },
                }
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

        settings = get_settings()
        notebook = settings.workspace_host_root / Path(terminal["workspace"]["notebook_path"])
        artifact = notebook.parents[1] / "artifacts" / "other" / "result.txt"
        if not notebook.is_file() or artifact.read_text(encoding="utf-8") != "completed":
            raise RuntimeError("Expected notebook or artifact was not created.")
        print("execution_id:", execution_id)
        print("status:", terminal["state"]["status"])
        print("notebook:", notebook)
        print("step_statuses:", [step["result"]["status"] for step in terminal["steps"]])


if __name__ == "__main__":
    asyncio.run(main())
