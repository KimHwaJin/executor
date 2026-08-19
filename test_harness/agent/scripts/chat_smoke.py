"""Run one natural-language Agent -> Executor -> Jupyter scenario through Agent Server."""

import asyncio
import os

from langgraph_sdk import get_client


async def main() -> None:
    server_url = os.getenv("TEST_AGENT_SERVER_URL", "http://127.0.0.1:2024")
    graph_id = os.getenv("TEST_AGENT_GRAPH_ID", "executor_mcp_agent")
    prompt = os.getenv(
        "TEST_AGENT_CHAT_PROMPT",
        "basic 커널에서 1부터 10까지의 합계를 실제로 계산하고 출력해줘.",
    )
    client = get_client(url=server_url)
    thread = await client.threads.create()
    result = await client.runs.wait(
        thread["thread_id"],
        graph_id,
        input={"messages": [{"role": "user", "content": prompt}]},
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"Agent Server returned an unexpected result: {result}")
    if result.get("phase") != "SUCCEEDED":
        if result.get("phase") == "READY":
            raise RuntimeError(
                "The Agent returned READY without executing. Set TEST_AGENT_LLM_MODEL in "
                "test_harness/agent/.env and restart langgraph dev."
            )
        raise RuntimeError(f"Natural-language execution did not succeed: {result}")
    messages = result.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RuntimeError("Natural-language execution returned no messages.")
    final_message = messages[-1]
    if not isinstance(final_message, dict):
        raise RuntimeError("Natural-language execution returned an invalid final message.")
    print("thread_id:", thread["thread_id"])
    print("execution_id:", result.get("execution_id"))
    print("phase:", result["phase"])
    print("response:")
    print(final_message.get("content", ""))


if __name__ == "__main__":
    asyncio.run(main())
