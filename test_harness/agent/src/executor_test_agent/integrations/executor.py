"""MCP-only client for Executor public tools used by the test Agent."""

from typing import Any

from mcp import Client


class ExecutorToolError(RuntimeError):
    """Raised when an Executor MCP tool returns an error or no structured result."""


async def required_tool_result(
    client: Client, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    if result.is_error or result.structured_content is None:
        raise ExecutorToolError(f"{tool} failed: {result.content}")
    return result.structured_content


async def collect_execution_result(client: Client, execution_id: str) -> dict[str, Any]:
    return await required_tool_result(
        client,
        "execution_result_get",
        {"execution_id": execution_id},
    )
