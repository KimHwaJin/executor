"""Verify FAILED execution resumes from its failed cell on the retained kernel."""

import asyncio
from typing import Any
from uuid import uuid4

from mcp import Client


async def _wait(client: Client, execution_id: str, terminal: set[str]) -> dict[str, Any]:
    for _ in range(200):
        result = await client.call_tool("execution_get", {"execution_id": execution_id})
        state = result.structured_content
        if state["status"] in terminal:
            return state
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Execution {execution_id} did not reach {terminal}.")


async def main() -> None:
    unique = str(uuid4())
    async with Client("http://127.0.0.1:8000/mcp") as client:
        submitted = await client.call_tool(
            "execution_submit",
            {
                "request": {
                    "idempotency_key": f"retry-submit-{unique}",
                    "mode": "STATIC",
                    "trigger_type": "INTERACTIVE",
                    "jupyter_pool": "INTERACTIVE",
                    "kernel_name": "python3",
                    "source": {
                        "type": "INLINE",
                        "code": (
                            "# %%\n"
                            "from pathlib import Path\n"
                            "attempt_counter = 0\n"
                            "# %%\n"
                            "attempt_counter += 1\n"
                            "Path('artifacts/other/retry-state.txt').write_text(\n"
                            "    str(attempt_counter), encoding='utf-8'\n"
                            ")\n"
                            "if attempt_counter == 1:\n"
                            "    raise RuntimeError('expected first-attempt failure')\n"
                            "print(attempt_counter)\n"
                            "# %%\n"
                            "print('retry completed')\n"
                        ),
                    },
                    "context": {
                        "requested_by_user_id": "retry-user",
                        "project_id": "retry-project",
                        "session_id": "retry-session",
                        "execution_plan_id": f"retry-plan-{unique}",
                    },
                    "steps": [
                        {"sequence": 0, "tool_name": "initialize"},
                        {"sequence": 1, "tool_name": "fail_once"},
                        {"sequence": 2, "tool_name": "finish"},
                    ],
                }
            },
        )
        execution_id = submitted.structured_content["execution_id"]
        failed = await _wait(client, execution_id, {"FAILED"})
        if not failed["retryable"] or failed["retry_from_sequence"] != 1:
            raise RuntimeError(f"Failure was not resumable: {failed}")
        original_kernel = failed["kernel_id"]
        original_server = failed["jupyter_server_id"]

        retry = await client.call_tool(
            "execution_retry",
            {
                "request": {
                    "execution_id": execution_id,
                    "idempotency_key": f"retry-command-{unique}",
                }
            },
        )
        if retry.is_error or retry.structured_content["status"] != "QUEUED":
            raise RuntimeError(f"Retry was not queued: {retry.content}")

        succeeded = await _wait(client, execution_id, {"SUCCEEDED", "FAILED"})
        if (
            succeeded["status"] != "SUCCEEDED"
            or succeeded["retry_count"] != 1
            or succeeded["kernel_id"] != original_kernel
            or succeeded["jupyter_server_id"] != original_server
            or [step["status"] for step in succeeded["steps"]]
            != ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"]
        ):
            raise RuntimeError(f"Retained-kernel retry failed: {succeeded}")

        trace_result = await client.call_tool(
            "execution_trace_get", {"execution_id": execution_id}
        )
        if trace_result.is_error:
            raise RuntimeError(str(trace_result.content))
        trace = trace_result.structured_content
        attempts = trace["attempts"]
        if (
            len(attempts) != 2
            or [step["status"] for step in attempts[0]["steps"]]
            != ["SUCCEEDED", "FAILED"]
            or [step["sequence"] for step in attempts[1]["steps"]] != [1, 2]
            or [step["status"] for step in attempts[1]["steps"]]
            != ["SUCCEEDED", "SUCCEEDED"]
        ):
            raise RuntimeError(f"Attempt Step history is incomplete: {attempts}")
        event_types = {event["event_type"] for event in trace["events"]}
        required_events = {
            "execution.submitted",
            "execution.started",
            "execution.failed",
            "execution.retry_requested",
            "execution.succeeded",
        }
        if not required_events.issubset(event_types):
            raise RuntimeError(f"Execution event history is incomplete: {event_types}")
        retry_artifacts = [
            artifact
            for artifact in trace["artifacts"]
            if artifact["name"] == "retry-state.txt"
        ]
        if (
            [artifact["status"] for artifact in retry_artifacts]
            != ["INCOMPLETE", "AVAILABLE"]
            or retry_artifacts[0]["execution_attempt_id"]
            == retry_artifacts[1]["execution_attempt_id"]
        ):
            raise RuntimeError(
                f"Retry Artifact history was not preserved: {retry_artifacts}"
            )

        print("execution_id:", execution_id)
        print("initial_status:", failed["status"])
        print("retry_status:", succeeded["status"])
        print("retry_from_sequence:", failed["retry_from_sequence"])
        print("same_kernel:", succeeded["kernel_id"] == original_kernel)
        print("attempts_in_trace:", len(attempts))
        print("events_in_trace:", len(trace["events"]))
        print("retry_artifact_statuses:", [item["status"] for item in retry_artifacts])


if __name__ == "__main__":
    asyncio.run(main())
