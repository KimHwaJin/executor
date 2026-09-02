"""Run the deterministic MULTI Agent -> Executor -> Jupyter scenario through Agent Server."""

import asyncio
import os
from typing import Any
from uuid import uuid4

from langgraph_sdk import get_client

from executor_test_agent.config import get_settings
from executor_test_agent.integrations.events import ExecutionEventWaiter


async def main() -> None:
    settings = get_settings()
    server_url = os.getenv("TEST_AGENT_SERVER_URL", "http://127.0.0.1:2024")
    client = get_client(url=server_url)
    thread = await client.threads.create()
    unique = uuid4().hex
    waiter = ExecutionEventWaiter(
        settings.executor_redis_url,
        settings.executor_event_stream,
        settings.executor_consumer_group_prefix,
        executor_mcp_url=settings.executor_mcp_url,
    )
    await waiter.open()
    try:
        interrupted = await client.runs.wait(
            thread["thread_id"],
            "executor_mcp_agent",
            input={
                "messages": [
                    {
                        "role": "user",
                        "content": "Run the deterministic Executor/Jupyter integration scenario.",
                    }
                ],
                "phase": "BOOTSTRAP",
                "execution_id": None,
                "execution_request": {
                    "runtime_profile": os.getenv("TEST_AGENT_RUNTIME_PROFILE", "default"),
                    "user_id": "agent-e2e-user",
                    "project_id": "agent-e2e-project",
                    "session_id": f"agent-e2e-session-{unique}",
                    "task_id": f"agent-e2e-task-{unique}",
                    "operation_mode": "MULTI",
                    "steps": [
                        {
                            "skill_name": "eda",
                            "tool_name": "sum_values",
                            "code": "values = [2, 3, 5]\ntotal = sum(values)\nprint(total)",
                        },
                        {
                            "skill_name": "eda",
                            "tool_name": "double_total",
                            "code": "doubled = total * 2\nprint(doubled)",
                        },
                    ],
                    "follow_up_operations": [
                        [
                            {
                                "skill_name": "report",
                                "tool_name": "write_agent_result",
                                "code": (
                                    "from pathlib import Path\n"
                                    "assert total == 10\n"
                                    "assert doubled == 20\n"
                                    "Path('artifacts/reports/agent-e2e.txt').write_text("
                                    "str(doubled), encoding='utf-8')\n"
                                    "print('agent artifact written')"
                                ),
                            }
                        ]
                    ],
                    "auto_finalize": True,
                },
                "event_batch": None,
                "execution_result": None,
            },
        )
        if not isinstance(interrupted, dict) or interrupted.get("phase") != "WAITING_FOR_EVENT":
            raise RuntimeError(f"Agent did not interrupt after submission: {interrupted}")
        execution_id = interrupted.get("execution_id")
        if not isinstance(execution_id, str):
            raise RuntimeError("Interrupted Agent state has no execution_id.")
        result: dict[str, Any] = interrupted
        last_event_sequence = 0
        for _ in range(5):
            if result.get("phase") != "WAITING_FOR_EVENT":
                break
            event_types = result.get("awaited_event_types")
            if not isinstance(event_types, list) or not event_types:
                raise RuntimeError(f"Agent supplied no wake-up event types: {result}")
            batch = await waiter.wait_for_wakeup(
                execution_id,
                timeout_seconds=settings.execution_timeout_seconds,
                event_types=set(event_types),
                operation_id=result.get("awaited_operation_id"),
                after_sequence=last_event_sequence,
            )
            last_event_sequence = batch.wake_event.event_sequence
            resumed = await client.runs.wait(
                thread["thread_id"],
                "executor_mcp_agent",
                command={"resume": batch.model_dump(mode="json")},
            )
            if not isinstance(resumed, dict):
                raise RuntimeError(f"Agent returned an invalid resumed state: {resumed}")
            result = resumed
        else:
            raise RuntimeError("Agent MULTI scenario exceeded its expected checkpoint count.")
    finally:
        await waiter.close()

    if not isinstance(result, dict):
        raise RuntimeError(f"Agent Server returned an unexpected result: {result}")
    execution = result.get("execution_result")
    if result.get("phase") != "SUCCEEDED" or not isinstance(execution, dict):
        raise RuntimeError(f"Agent integration run failed: {result}")
    artifact_names = {artifact["name"] for artifact in execution["artifacts"]}
    required = {"agent-e2e.txt", "execution.ipynb"}
    if not required.issubset(artifact_names):
        raise RuntimeError(f"Required Jupyter artifacts are missing: {artifact_names}")
    if len(execution["notebook"]["cells"]) != 3:
        raise RuntimeError("Agent did not retrieve all three executed Jupyter notebook cells.")
    event_types = [event["event_type"] for event in result.get("event_history", [])]
    if event_types.count("execution.operation_completed") != 2:
        raise RuntimeError(f"Agent did not cross two MULTI Operation boundaries: {event_types}")
    if event_types.count("execution.step_completed") != 3:
        raise RuntimeError(f"Agent did not checkpoint all Step results: {event_types}")
    receipts = result.get("command_receipts", [])
    if len(receipts) != 3:
        raise RuntimeError(f"Expected submit, Operation, and finalize receipts: {receipts}")

    stream_thread = await client.threads.create()
    stream_unique = uuid4().hex
    stream_result = await client.runs.wait(
        stream_thread["thread_id"],
        "executor_mcp_agent",
        input={
            "messages": [
                {
                    "role": "user",
                    "content": "Run the self-contained MULTI stream scenario.",
                }
            ],
            "wait_strategy": "STREAM",
            "execution_request": {
                "runtime_profile": os.getenv("TEST_AGENT_RUNTIME_PROFILE", "default"),
                "user_id": "agent-stream-user",
                "project_id": "agent-stream-project",
                "session_id": f"agent-stream-session-{stream_unique}",
                "task_id": f"agent-stream-task-{stream_unique}",
                "operation_mode": "MULTI",
                "steps": [
                    {
                        "skill_name": "eda",
                        "tool_name": "create_stream_value",
                        "code": "stream_value = 7\nprint(stream_value)",
                    }
                ],
                "follow_up_operations": [
                    [
                        {
                            "skill_name": "eda",
                            "tool_name": "reuse_stream_value",
                            "code": "stream_result = stream_value + 1\nprint(stream_result)",
                        }
                    ]
                ],
                "auto_finalize": True,
            },
        },
    )
    if not isinstance(stream_result, dict) or stream_result.get("phase") != "SUCCEEDED":
        raise RuntimeError(f"Self-contained MULTI stream run failed: {stream_result}")
    stream_event_types = [event["event_type"] for event in stream_result.get("event_history", [])]
    if stream_event_types.count("execution.operation_completed") != 2:
        raise RuntimeError(
            "Self-contained stream run did not cross exactly two current Operation boundaries: "
            f"{stream_event_types}"
        )
    if stream_event_types.count("execution.step_completed") != 2:
        raise RuntimeError(
            f"Self-contained stream run did not retain both Step results: {stream_event_types}"
        )
    print("execution_id:", execution["execution_id"])
    print("status:", execution["status"])
    print("wake_event_type:", execution["wake_event_type"])
    print("operation_count:", 2)
    print("step_event_count:", event_types.count("execution.step_completed"))
    print("runtime_target_id:", execution["runtime_target_id"])
    print("notebook_path:", execution["notebook_path"])
    print("artifacts:", sorted(artifact_names))
    print("stream_execution_id:", stream_result["execution_id"])
    print("stream_boundary_filter:", "verified")


if __name__ == "__main__":
    asyncio.run(main())
