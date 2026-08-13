"""Stream one run from the local LangGraph development server."""

import asyncio
import os

from langgraph_sdk import get_client


async def main() -> None:
    server_url = os.getenv("TEST_AGENT_SERVER_URL", "http://127.0.0.1:2024")
    client = get_client(url=server_url)
    thread = await client.threads.create()
    async for chunk in client.runs.stream(
        thread["thread_id"],
        "executor_test_agent",
        input={
            "messages": [
                {
                    "role": "user",
                    "content": "Confirm that the Executor test Agent server is ready.",
                }
            ],
            "phase": "BOOTSTRAP",
            "execution_id": None,
        },
        stream_mode="updates",
    ):
        print(chunk.event, chunk.data)


if __name__ == "__main__":
    asyncio.run(main())
