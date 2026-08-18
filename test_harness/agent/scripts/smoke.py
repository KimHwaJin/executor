"""Run the deterministic Agent -> Executor -> Jupyter scenario through Agent Server."""

import asyncio
import os
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
    )
    await waiter.open()
    try:
        interrupted = await client.runs.wait(
            thread["thread_id"],
            "executor_test_agent",
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
                    "runtime_profile": os.getenv("TEST_AGENT_RUNTIME_PROFILE", "basic"),
                    "user_id": "agent-e2e-user",
                    "project_id": "agent-e2e-project",
                    "session_id": f"agent-e2e-session-{unique}",
                    "task_id": f"agent-e2e-task-{unique}",
                    "steps": [
                        {
                            "skill_name": "eda",
                            "tool_name": "sum_values",
                            "code": "values = [2, 3, 5]\ntotal = sum(values)\nprint(total)",
                        },
                        {
                            "skill_name": "report",
                            "tool_name": "write_agent_result",
                            "code": (
                                "from pathlib import Path\n"
                                "assert total == 10\n"
                                "Path('artifacts/reports/agent-e2e.txt').write_text("
                                "str(total), encoding='utf-8')\n"
                                "print('agent artifact written')"
                            ),
                        },
                    ],
                },
                "terminal_event": None,
                "execution_result": None,
            },
        )
        if not isinstance(interrupted, dict) or interrupted.get("phase") != "WAITING_FOR_EVENT":
            raise RuntimeError(f"Agent did not interrupt after submission: {interrupted}")
        execution_id = interrupted.get("execution_id")
        if not isinstance(execution_id, str):
            raise RuntimeError("Interrupted Agent state has no execution_id.")
        terminal_event = await waiter.wait_for_terminal(
            execution_id,
            timeout_seconds=settings.execution_timeout_seconds,
        )
        result = await client.runs.wait(
            thread["thread_id"],
            "executor_test_agent",
            command={"resume": terminal_event.model_dump(mode="json")},
        )
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
    if len(execution["notebook"]["cells"]) != 2:
        raise RuntimeError("Agent did not retrieve the two executed Jupyter notebook cells.")
    print("execution_id:", execution["execution_id"])
    print("status:", execution["status"])
    print("terminal_event_type:", execution["terminal_event_type"])
    print("runtime_target_id:", execution["runtime_target_id"])
    print("notebook_path:", execution["notebook_path"])
    print("artifacts:", sorted(artifact_names))


if __name__ == "__main__":
    asyncio.run(main())
