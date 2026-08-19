"""Run the planning Agent through plan generation, HITL approval, and Executor completion."""

import asyncio
import json
import os

from langgraph_sdk import get_client


async def main() -> None:
    server_url = os.getenv("TEST_AGENT_SERVER_URL", "http://127.0.0.1:2024")
    prompt = os.getenv(
        "TEST_AGENT_CHAT_PROMPT",
        "basic 커널에서 1부터 10까지의 합계를 실제로 계산하고 출력해줘.",
    )
    decision = os.getenv("TEST_AGENT_PLAN_DECISION", "approve").strip().lower()
    if decision not in {"approve", "reject"}:
        raise ValueError("TEST_AGENT_PLAN_DECISION must be approve or reject.")

    client = get_client(url=server_url)
    thread = await client.threads.create()
    interrupted = await client.runs.wait(
        thread["thread_id"],
        "executor_planning_agent",
        input={"messages": [{"role": "user", "content": prompt}]},
    )
    if not isinstance(interrupted, dict):
        raise RuntimeError(f"Planning Agent returned an invalid state: {interrupted}")
    interrupts = interrupted.get("__interrupt__")
    if not isinstance(interrupts, list) or not interrupts:
        raise RuntimeError(f"Planning Agent did not request plan review: {interrupted}")
    interrupt_value = interrupts[0].get("value")
    if not isinstance(interrupt_value, dict):
        raise RuntimeError(f"Planning Agent returned an invalid HITL payload: {interrupts[0]}")
    print("thread_id:", thread["thread_id"])
    print("plan_review:")
    print(json.dumps(interrupt_value, ensure_ascii=False, indent=2))

    response = {"decisions": [{"type": decision}]}
    result = await client.runs.wait(
        thread["thread_id"],
        "executor_planning_agent",
        command={"resume": response},
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"Planning Agent returned an invalid resumed state: {result}")
    expected_phase = "CANCELLED" if decision == "reject" else "SUCCEEDED"
    if result.get("phase") != expected_phase:
        raise RuntimeError(f"Planning Agent ended unexpectedly: {result}")
    messages = result.get("messages")
    final_message = messages[-1] if isinstance(messages, list) and messages else {}
    print("decision:", decision)
    print("execution_id:", result.get("execution_id"))
    print("phase:", result.get("phase"))
    print("response:")
    print(final_message.get("content", "") if isinstance(final_message, dict) else final_message)


if __name__ == "__main__":
    asyncio.run(main())
