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
    result = await required_tool_result(
        client,
        "execution_result_get",
        {"execution_id": execution_id},
    )
    outputs = await _collect_outputs(client, execution_id)
    by_result_ref: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for output in outputs:
        producer = output["produced_by"]
        key = (str(producer["step_id"]), str(producer["attempt_id"]))
        by_result_ref.setdefault(key, []).append(output)
    for operation in result["operations"]:
        for step in operation["steps"]:
            reference = step["result"].get("result_ref")
            step["result"]["resolved_outputs"] = (
                by_result_ref.get(
                    (str(reference["step_id"]), str(reference["attempt_id"])),
                    [],
                )
                if reference is not None
                else []
            )
    return result


async def _collect_outputs(client: Client, execution_id: str) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = await required_tool_result(
            client,
            "execution_output_list",
            {
                "execution_id": execution_id,
                "cursor": cursor,
                "limit": 200,
            },
        )
        for output in page["items"]:
            await _resolve_small_text_representations(client, execution_id, output)
            outputs.append(output)
        cursor = page.get("next_cursor")
        if cursor is None:
            return outputs


async def _resolve_small_text_representations(
    client: Client, execution_id: str, output: dict[str, Any]
) -> None:
    for representation in output["representations"]:
        if not str(representation["media_type"]).startswith("text/"):
            continue
        content = await required_tool_result(
            client,
            "execution_output_content_get",
            {
                "execution_id": execution_id,
                "output_id": output["output_id"],
                "representation_id": representation["representation_id"],
            },
        )
        representation["delivery"] = content["delivery"]
        representation["content_url"] = content["content_url"]
        representation["content"] = content.get("content")
