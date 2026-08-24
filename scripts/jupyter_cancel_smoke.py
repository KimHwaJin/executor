"""Verify that MCP cancellation interrupts a running Jupyter kernel."""

import asyncio
from uuid import uuid4

from execution_spec_payload import execution_request, inline_spec
from mcp import Client


async def main() -> None:
    unique = str(uuid4())
    async with Client("http://127.0.0.1:8000/mcp") as client:
        submitted = await client.call_tool(
            "execution_submit",
            {
                "request": execution_request(
                    idempotency_key=f"cancel-smoke-submit-{unique}",
                    operation_mode="SINGLE",
                    trigger_type="INTERACTIVE",
                    actor={"type": "USER", "id": "smoke-user"},
                    runtime_profile="basic",
                    spec=inline_spec(
                        [
                            {
                                "tool_name": "long_running",
                                "code": (
                                    "import time\nprint('started')\ntime.sleep(30)\n"
                                    "print('finished')"
                                ),
                            }
                        ],
                    ),
                    context={
                        "user_id": "smoke-user",
                        "project_id": "smoke-project",
                        "session_id": "smoke-session",
                        "task_id": f"cancel-task-{unique}",
                    },
                )
            },
        )
        execution_id = submitted.structured_content["execution_id"]
        for _ in range(100):
            current = await client.call_tool(
                "execution_get", {"execution_id": execution_id}
            )
            if current.structured_content["state"]["status"] == "RUNNING":
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
                    "actor": {"type": "USER", "id": "smoke-user"},
                }
            },
        )
        if (
            cancelled.structured_content["state"]["status"]
            != "CANCEL_REQUESTED"
        ):
            raise RuntimeError("Cancellation request was not recorded.")
        for _ in range(100):
            current = await client.call_tool(
                "execution_get", {"execution_id": execution_id}
            )
            if current.structured_content["state"]["status"] == "CANCELLED":
                print("execution_id:", execution_id)
                print("status: CANCELLED")
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(
            f"Execution was not cancelled: {current.structured_content}"
        )


if __name__ == "__main__":
    asyncio.run(main())
