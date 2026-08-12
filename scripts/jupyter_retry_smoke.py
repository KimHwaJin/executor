"""Verify FAILED execution resumes from its failed cell on the retained kernel."""

import asyncio
from typing import Any
from uuid import uuid4

from execution_spec_payload import inline_source
from mcp import Client


async def _wait(client: Client, execution_id: str, terminal: set[str]) -> dict[str, Any]:
    for _ in range(200):
        result = await client.call_tool("execution_get", {"execution_id": execution_id})
        state = result.structured_content
        if state["state"]["status"] in terminal:
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
                    "actor": {"type": "USER", "id": "retry-user"},
                    "runtime_profile": "basic",
                    "source": inline_source(
                        f"retry-plan-{unique}",
                        [
                            {
                                "tool_name": "initialize",
                                "code": "from pathlib import Path\nattempt_counter = 0",
                            },
                            {
                                "tool_name": "fail_once",
                                "code": (
                                    "attempt_counter += 1\n"
                                    "Path('artifacts/other/retry-state.txt').write_text(\n"
                                    "    str(attempt_counter), encoding='utf-8'\n"
                                    ")\n"
                                    "if attempt_counter == 1:\n"
                                    "    raise RuntimeError('expected first-attempt failure')\n"
                                    "print(attempt_counter)"
                                ),
                            },
                            {"tool_name": "finish", "code": "print('retry completed')"},
                        ],
                    ),
                    "context": {
                        "user_id": "retry-user",
                        "project_id": "retry-project",
                        "session_id": "retry-session",
                        "task_id": f"retry-task-{unique}",
                    },
                }
            },
        )
        execution_id = submitted.structured_content["execution_id"]
        failed = await _wait(client, execution_id, {"FAILED"})
        if (
            failed["failure"]["type"] != "TOOL_ERROR"
            or failed["retry"]["strategy"] != "FROM_FAILED_STEP"
            or failed["retry"]["from_sequence"] != 1
        ):
            raise RuntimeError(f"Failure was not resumable: {failed}")
        original_runtime_session = failed["runtime"]["session_id"]
        original_runtime_target = failed["runtime"]["target_id"]

        retry = await client.call_tool(
            "execution_retry",
            {
                "request": {
                    "execution_id": execution_id,
                    "idempotency_key": f"retry-command-{unique}",
                    "actor": {"type": "USER", "id": "retry-user"},
                }
            },
        )
        if retry.is_error or retry.structured_content["state"]["status"] != "QUEUED":
            raise RuntimeError(f"Retry was not queued: {retry.content}")

        succeeded = await _wait(client, execution_id, {"SUCCEEDED", "FAILED"})
        if (
            succeeded["state"]["status"] != "SUCCEEDED"
            or succeeded["retry"]["count"] != 1
            or succeeded["runtime"]["session_id"] != original_runtime_session
            or succeeded["runtime"]["target_id"] != original_runtime_target
            or [step["result"]["status"] for step in succeeded["steps"]]
            != ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"]
        ):
            raise RuntimeError(f"Retained-kernel retry failed: {succeeded}")

        attempts_result = await client.call_tool(
            "execution_attempt_list", {"execution_id": execution_id}
        )
        events_result = await client.call_tool(
            "execution_event_list", {"execution_id": execution_id}
        )
        artifacts_result = await client.call_tool(
            "execution_artifact_list", {"execution_id": execution_id}
        )
        attempts = attempts_result.structured_content["items"]
        if (
            len(attempts) != 2
            or attempts[0]["failure"]["type"] != "TOOL_ERROR"
            or attempts[0]["recovery"]["retry_strategy"] != "FROM_FAILED_STEP"
            or [step["result"]["status"] for step in attempts[0]["steps"]]
            != ["SUCCEEDED", "FAILED"]
            or [step["sequence"] for step in attempts[1]["steps"]] != [1, 2]
            or [step["result"]["status"] for step in attempts[1]["steps"]]
            != ["SUCCEEDED", "SUCCEEDED"]
        ):
            raise RuntimeError(f"Attempt Step history is incomplete: {attempts}")
        event_types = {event["event_type"] for event in events_result.structured_content["items"]}
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
            for artifact in artifacts_result.structured_content["items"]
            if artifact["name"] == "retry-state.txt"
        ]
        if [artifact["status"] for artifact in retry_artifacts] != [
            "INCOMPLETE",
            "AVAILABLE",
        ] or retry_artifacts[0]["produced_by"]["execution_attempt_id"] == retry_artifacts[1][
            "produced_by"
        ][
            "execution_attempt_id"
        ]:
            raise RuntimeError(f"Retry Artifact history was not preserved: {retry_artifacts}")

        print("execution_id:", execution_id)
        print("initial_status:", failed["state"]["status"])
        print("retry_status:", succeeded["state"]["status"])
        print("retry_from_sequence:", failed["retry"]["from_sequence"])
        print("same_kernel:", succeeded["runtime"]["session_id"] == original_runtime_session)
        print("attempts_in_trace:", len(attempts))
        print("event_count:", len(events_result.structured_content["items"]))
        print("retry_artifact_statuses:", [item["status"] for item in retry_artifacts])


if __name__ == "__main__":
    asyncio.run(main())
