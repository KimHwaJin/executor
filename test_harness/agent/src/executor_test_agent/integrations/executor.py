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
    execution = await required_tool_result(client, "execution_get", {"execution_id": execution_id})
    steps = await _collect_items(
        client,
        "execution_step_list",
        {"execution_id": execution_id},
        limit=200,
    )
    artifacts = await _collect_items(
        client,
        "execution_artifact_list",
        {"execution_id": execution_id},
        limit=500,
    )
    notebook = await required_tool_result(
        client,
        "execution_notebook_read",
        {
            "execution_id": execution_id,
            "response_format": "detailed",
            "start_index": 0,
            "limit": 0,
        },
    )
    return {
        "execution": execution,
        "steps": steps,
        "artifacts": artifacts,
        "notebook": notebook,
    }


async def _collect_items(
    client: Client,
    tool: str,
    arguments: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page_arguments = {**arguments, "limit": limit}
        if cursor is not None:
            page_arguments["cursor"] = cursor
        page = await required_tool_result(client, tool, page_arguments)
        items.extend(page["items"])
        cursor = page.get("next_cursor")
        if cursor is None:
            return items
