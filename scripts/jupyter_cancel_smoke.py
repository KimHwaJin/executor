"""Verify that MCP cancellation interrupts a running Jupyter kernel."""

import asyncio
from uuid import uuid4

from mcp import Client


async def main() -> None:
    unique = str(uuid4())
    async with Client("http://127.0.0.1:8000/mcp") as client:
        submitted = await client.call_tool(
            "execution_submit",
            {
                "request": {
                    "idempotency_key": f"cancel-smoke-submit-{unique}",
                    "mode": "STATIC",
                    "trigger_type": "INTERACTIVE",
                    "jupyter_pool": "INTERACTIVE",
                    "kernel_name": "python3",
                    "source": {
                        "type": "INLINE",
                        "code": "import time\nprint('started')\ntime.sleep(30)\nprint('finished')",
                    },
                    "context": {
                        "requested_by_user_id": "smoke-user",
                        "project_id": "smoke-project",
                        "session_id": "smoke-session",
                        "execution_plan_id": f"cancel-plan-{unique}",
                    },
                }
            },
        )
        execution_id = submitted.structured_content["execution_id"]
        for _ in range(100):
            current = await client.call_tool("execution_get", {"execution_id": execution_id})
            if current.structured_content["status"] == "RUNNING":
                break
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError("Execution did not enter RUNNING state.")

        cancelled = await client.call_tool(
            "execution_cancel",
            {
                "request": {
                    "execution_id": execution_id,
                    "idempotency_key": f"cancel-smoke-cancel-{unique}",
                    "reason": "cancel smoke test",
                }
            },
        )
        if cancelled.structured_content["status"] != "CANCEL_REQUESTED":
            raise RuntimeError("Cancellation request was not recorded.")
        for _ in range(100):
            current = await client.call_tool("execution_get", {"execution_id": execution_id})
            if current.structured_content["status"] == "CANCELLED":
                print("execution_id:", execution_id)
                print("status: CANCELLED")
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(f"Execution was not cancelled: {current.structured_content}")


if __name__ == "__main__":
    asyncio.run(main())
