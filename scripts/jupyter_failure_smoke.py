"""Verify Jupyter errors become FAILED with a durable error notebook."""

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
                    "idempotency_key": f"failure-smoke-{unique}",
                    "mode": "STATIC",
                    "actor": {"type": "USER", "id": "smoke-user"},
                    "runtime_profile": "basic",
                    "source": inline_source(
                        f"failure-plan-{unique}",
                        [
                            {
                                "tool_name": "expected_failure",
                                "code": "raise ValueError('expected')",
                            }
                        ],
                    ),
                    "context": {
                        "user_id": "smoke-user",
                        "project_id": "smoke-project",
                        "session_id": "smoke-session",
                        "task_id": f"failure-task-{unique}",
                    },
                }
            },
        )
        execution_id = submitted.structured_content["execution_id"]
        current = submitted
        for _ in range(100):
            current = await client.call_tool("execution_get", {"execution_id": execution_id})
            if current.structured_content["state"]["status"] == "FAILED":
                break
            await asyncio.sleep(0.1)
        result = current.structured_content
        steps_result = await client.call_tool(
            "execution_step_list", {"execution_id": execution_id, "limit": 100}
        )
        if steps_result.is_error:
            raise RuntimeError(str(steps_result.content))
        steps = steps_result.structured_content["items"]
        if (
            result["state"]["status"] != "FAILED"
            or steps[0]["result"]["status"] != "FAILED"
        ):
            raise RuntimeError(f"Expected FAILED execution and step: {result}")
        notebook = get_settings().workspace_host_root / Path(result["workspace"]["notebook_path"])
        if not notebook.is_file():
            raise RuntimeError("Failure notebook was not created.")
        print("execution_id:", execution_id)
        print("status: FAILED")
        print("error:", result["failure"]["message"])


if __name__ == "__main__":
    asyncio.run(main())
