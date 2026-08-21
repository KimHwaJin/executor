"""Tests for consolidated Executor MCP result collection used by the test Agent."""

from typing import Any, cast

from executor_test_agent.integrations import executor as executor_module


async def test_collect_execution_result_uses_one_consolidated_tool_call(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_required_result(_client, tool, arguments):
        calls.append((tool, arguments))
        return {"execution": {"execution_id": "execution-1"}, "operations": []}

    monkeypatch.setattr(executor_module, "required_tool_result", fake_required_result)

    result = await executor_module.collect_execution_result(cast(Any, object()), "execution-1")

    assert result["execution"]["execution_id"] == "execution-1"
    assert calls == [("execution_result_get", {"execution_id": "execution-1"})]
