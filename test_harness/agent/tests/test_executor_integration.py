"""Tests for Executor MCP result collection used by the test Agent."""

from typing import Any, cast

from executor_test_agent.integrations import executor as executor_module


async def test_collect_items_follows_every_opaque_cursor(monkeypatch) -> None:
    calls: list[dict] = []

    async def fake_required_result(_client, tool, arguments):
        assert tool == "execution_step_list"
        calls.append(arguments)
        if "cursor" not in arguments:
            return {"items": [{"sequence": 0}], "next_cursor": "opaque:next"}
        assert arguments["cursor"] == "opaque:next"
        return {"items": [{"sequence": 1}], "next_cursor": None}

    monkeypatch.setattr(executor_module, "required_tool_result", fake_required_result)

    items = await executor_module._collect_items(
        cast(Any, object()),
        "execution_step_list",
        {"execution_id": "execution-1"},
        limit=200,
    )

    assert [item["sequence"] for item in items] == [0, 1]
    assert calls == [
        {"execution_id": "execution-1", "limit": 200},
        {"execution_id": "execution-1", "limit": 200, "cursor": "opaque:next"},
    ]
