"""Submit a STATIC execution through MCP and wait for its Jupyter result."""

import asyncio
from pathlib import Path
from uuid import uuid4

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
                    "jupyter_pool": "INTERACTIVE",
                    "kernel_name": "python3",
                    "source": {
                        "type": "INLINE",
                        "code": (
                            "# %%\n"
                            "numbers = [1, 2, 3]\n"
                            "print(sum(numbers))\n"
                            "# %%\n"
                            "from pathlib import Path\n"
                            "Path('artifacts/other/result.txt').write_text("
                            "'completed', encoding='utf-8')\n"
                            "len(numbers)\n"
                        ),
                    },
                    "context": {
                        "requested_by_user_id": "smoke-user",
                        "project_id": "smoke-project",
                        "session_id": "smoke-session",
                        "execution_plan_id": f"smoke-plan-{unique}",
                    },
                    "steps": [
                        {"sequence": 0, "skill_name": "eda", "tool_name": "sum_values"},
                        {"sequence": 1, "skill_name": "report", "tool_name": "save_result"},
                    ],
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
            if terminal["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                break
            await asyncio.sleep(0.2)
        if terminal is None or terminal["status"] != "SUCCEEDED":
            raise RuntimeError(f"Execution did not succeed: {terminal}")

        settings = get_settings()
        notebook = settings.workspace_host_root / Path(terminal["notebook_path"])
        artifact = notebook.parents[1] / "artifacts" / "other" / "result.txt"
        if not notebook.is_file() or artifact.read_text(encoding="utf-8") != "completed":
            raise RuntimeError("Expected notebook or artifact was not created.")
        print("execution_id:", execution_id)
        print("status:", terminal["status"])
        print("notebook:", notebook)
        print("step_statuses:", [step["status"] for step in terminal["steps"]])


if __name__ == "__main__":
    asyncio.run(main())
