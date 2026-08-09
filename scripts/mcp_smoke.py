"""Smoke test a running Streamable HTTP server using the official MCP v2 client."""

import asyncio
from uuid import uuid4

from mcp import Client


async def main() -> None:
    submit_key = f"smoke-submit-{uuid4()}"
    cancel_key = f"smoke-cancel-{uuid4()}"
    async with Client("http://127.0.0.1:8000/mcp") as client:
        listed = await client.list_tools()
        print("tools:", [tool.name for tool in listed.tools])

        submitted = await client.call_tool(
            "execution_submit",
            {
                "request": {
                    "idempotency_key": submit_key,
                    "mode": "STATIC",
                    "trigger_type": "INTERACTIVE",
                    "jupyter_pool": "INTERACTIVE",
                    "kernel_name": "python-analysis-a",
                    "source": {"type": "INLINE", "code": "print('smoke')"},
                    "context": {
                        "requested_by_user_id": "smoke-user",
                        "project_id": "smoke-project",
                        "session_id": "smoke-session",
                        "execution_plan_id": "smoke-plan",
                    },
                }
            },
        )
        execution_id = submitted.structured_content["execution_id"]
        print("submitted:", submitted.structured_content)

        fetched = await client.call_tool("execution_get", {"execution_id": execution_id})
        print("fetched:", fetched.structured_content)

        cancelled = await client.call_tool(
            "execution_cancel",
            {
                "request": {
                    "execution_id": execution_id,
                    "idempotency_key": cancel_key,
                    "reason": "smoke test",
                }
            },
        )
        print("cancelled:", cancelled.structured_content)


if __name__ == "__main__":
    asyncio.run(main())
