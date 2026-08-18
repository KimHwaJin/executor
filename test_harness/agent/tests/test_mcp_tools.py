"""Executor MCP Tool allowlist and policy wrapper tests."""

import json

import pytest
from mcp.types import CallToolResult, TextContent

from executor_test_agent.config import AgentSettings
from executor_test_agent.mcp_tools import (
    ADMIN_TOOL_NAMES,
    MUTATION_MCP_TOOL_NAMES,
    READ_TOOL_NAMES,
    _actor,
    _all_execution_steps,
    _enforce_read_scope,
    _idempotency_key,
    _mutation_result,
    render_tool_result,
)


@pytest.fixture
def settings() -> AgentSettings:
    return AgentSettings(
        llm_base_url="http://llm/v1",
        llm_model="model",
        llm_api_key="secret",
        executor_mcp_url="http://executor/mcp",
        executor_redis_url="redis://redis/0",
        executor_event_stream="executor.events",
        executor_consumer_group_prefix="agent",
        execution_timeout_seconds=120,
        natural_language_execution_enabled=True,
        default_user_id="user-1",
        default_project_id="project-1",
    )


def test_allowlist_has_no_runtime_admin_tools() -> None:
    assert len(READ_TOOL_NAMES) == 16
    assert len(MUTATION_MCP_TOOL_NAMES) == 5
    assert not READ_TOOL_NAMES & ADMIN_TOOL_NAMES
    assert not MUTATION_MCP_TOOL_NAMES & ADMIN_TOOL_NAMES


async def test_execution_list_is_scoped_to_configured_user(settings) -> None:
    assert await _enforce_read_scope("execution_list", {}, settings) == {"user_id": "user-1"}
    with pytest.raises(PermissionError, match="another user's"):
        await _enforce_read_scope("execution_list", {"user_id": "user-2"}, settings)


def test_idempotency_key_is_stable_per_request_scope() -> None:
    arguments = {"execution_id": "execution-1"}
    first = _idempotency_key("cancel", "thread-1", arguments)
    second = _idempotency_key("cancel", "thread-1", arguments)
    other_scope = _idempotency_key("cancel", "thread-2", arguments)

    assert first == second
    assert first != other_scope


def test_mutation_actor_is_the_agent_not_the_context_user(settings) -> None:
    assert _actor(settings) == {"type": "AGENT", "id": "executor-test-agent"}


def test_render_tool_result_prefers_structured_content() -> None:
    result = CallToolResult(
        content=[TextContent(type="text", text="fallback")],
        structured_content={"items": [{"runtime": {"supported_profiles": ["basic", "ml"]}}]},
    )

    rendered = json.loads(render_tool_result(result))
    assert rendered["items"][0]["runtime"]["supported_profiles"] == ["basic", "ml"]


def test_mutation_result_reads_nested_command_state() -> None:
    rendered = _mutation_result(
        {
            "execution_id": "execution-1",
            "operation": {
                "operation_id": "operation-1",
                "steps": [{"sequence": 0, "step_id": "step-1"}],
            },
            "state": {"status": "QUEUED", "version": 0},
        },
        wait_for_event=True,
        event_types=["execution.succeeded"],
        event_stream_start_id="123-0",
    )

    assert rendered["status"] == "QUEUED"
    assert rendered["version"] == 0
    assert rendered["operation"]["operation_id"] == "operation-1"
    assert rendered["operation"]["steps"][0]["step_id"] == "step-1"
    assert rendered["event_stream_start_id"] == "123-0"


async def test_all_execution_steps_follows_opaque_cursor(monkeypatch, settings) -> None:
    calls: list[dict] = []

    async def fake_call(_settings, tool, arguments):
        assert tool == "execution_step_list"
        calls.append(arguments)
        if "cursor" not in arguments:
            return {"items": [{"sequence": 0}], "next_cursor": "opaque:next"}
        assert arguments["cursor"] == "opaque:next"
        return {"items": [{"sequence": 1}], "next_cursor": None}

    monkeypatch.setattr("executor_test_agent.mcp_tools._call_mcp_structured", fake_call)

    items = await _all_execution_steps(settings, "execution-1")

    assert [item["sequence"] for item in items] == [0, 1]
    assert calls[1]["cursor"] == "opaque:next"
