"""Local graph contract tests that never call an external LLM."""

import json

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from executor_test_agent import graph as graph_module
from executor_test_agent.config import get_settings
from executor_test_agent.graph import BOOTSTRAP_MESSAGE, _chat_model, graph
from executor_test_agent.state import AgentState


async def test_graph_bootstraps_without_external_llm(monkeypatch) -> None:
    monkeypatch.delenv("TEST_AGENT_LLM_MODEL", raising=False)
    get_settings.cache_clear()
    _chat_model.cache_clear()

    raw_result = await graph.ainvoke(
        AgentState(
            messages=[HumanMessage(content="Are you ready?")],
            phase="BOOTSTRAP",
            execution_id=None,
        )
    )
    result = AgentState.model_validate(raw_result)

    assert result.phase == "READY"
    assert result.execution_id is None
    assert isinstance(result.messages[-1], AIMessage)
    assert result.messages[-1].content == BOOTSTRAP_MESSAGE


async def test_graph_runs_executor_request_and_returns_verified_result(monkeypatch) -> None:
    async def fake_submit_execution(request, _settings):
        assert request.runtime_profile == "basic"
        return "00000000-0000-0000-0000-000000000001"

    async def fake_reconcile_execution(execution_id, terminal_event, _settings):
        assert execution_id == "00000000-0000-0000-0000-000000000001"
        assert terminal_event.event_type == "execution.succeeded"
        return {
            "execution_id": execution_id,
            "status": "SUCCEEDED",
            "steps": [{"result": {"status": "SUCCEEDED"}}],
            "artifacts": [{"name": "result.txt"}],
            "notebook_path": "users/u/executions/execution-1/notebooks/execution.ipynb",
            "notebook": {
                "cells": [{"outputs": [{"output_type": "stream", "name": "stdout", "text": "3\n"}]}]
            },
        }

    monkeypatch.setattr(graph_module, "submit_execution", fake_submit_execution)
    monkeypatch.setattr(graph_module, "reconcile_execution", fake_reconcile_execution)
    checkpointed_graph = graph_module.builder.compile(checkpointer=MemorySaver())
    config = RunnableConfig(configurable={"thread_id": "graph-integration-test"})
    interrupted = await checkpointed_graph.ainvoke(
        AgentState(
            messages=[HumanMessage(content="Execute this plan")],
            execution_request={
                "runtime_profile": "basic",
                "user_id": "u",
                "project_id": "p",
                "session_id": "s",
                "task_id": "t",
                "execution_plan_id": "plan",
                "steps": [
                    {
                        "skill_name": "eda",
                        "tool_name": "sum_values",
                        "code": "print(sum([1, 2]))",
                    }
                ],
            },
        ),
        config,
    )
    assert interrupted["phase"] == "WAITING_FOR_EVENT"
    execution_id = interrupted["execution_id"]
    terminal_event = {
        "event_id": "00000000-0000-0000-0000-000000000002",
        "event_type": "execution.succeeded",
        "schema_version": "1.0",
        "aggregate_type": "Execution",
        "aggregate_id": execution_id,
        "occurred_at": "2026-08-13T00:00:00Z",
        "payload": {
            "schema_version": "1.0",
            "execution_id": execution_id,
            "status": "SUCCEEDED",
        },
    }
    raw_result = await checkpointed_graph.ainvoke(Command(resume=terminal_event), config)
    result = AgentState.model_validate(raw_result)

    assert result.phase == "SUCCEEDED"
    assert result.execution_id == execution_id
    assert result.execution_result is not None
    assert "result.txt" in str(result.messages[-1].content)


async def test_graph_runs_mcp_tool_agent_and_waits_for_stream_event(monkeypatch) -> None:
    execution_id = "00000000-0000-0000-0000-000000000010"
    terminal_event = {
        "event_id": "00000000-0000-0000-0000-000000000011",
        "event_type": "execution.succeeded",
        "schema_version": "1.0",
        "aggregate_type": "Execution",
        "aggregate_id": execution_id,
        "occurred_at": "2026-08-13T00:00:00Z",
        "payload": {
            "schema_version": "1.0",
            "execution_id": execution_id,
            "status": "SUCCEEDED",
        },
    }

    async def fake_load_tools(_settings, *, request_scope_id):
        assert request_scope_id.startswith("chat-test-")
        return [object()]

    class FakeToolAgent:
        async def ainvoke(self, state):
            original = state["messages"]
            mutation = {
                "execution_id": execution_id,
                "status": "QUEUED",
                "wait_for_event": True,
                "event_types": ["execution.succeeded", "execution.failed"],
            }
            return {
                "messages": [
                    *original,
                    AIMessage(content="", tool_calls=[]),
                    ToolMessage(
                        content=json.dumps(mutation),
                        tool_call_id="call-1",
                        name="execution_submit",
                    ),
                    AIMessage(content="실행을 제출했습니다."),
                ]
            }

    def fake_create_agent(_model, tools, *, system_prompt):
        assert len(tools) == 1
        assert "Executor operations Agent" in system_prompt
        return FakeToolAgent()

    async def fake_reconcile_execution(_execution_id, _terminal_event, _settings):
        return {
            "execution_id": execution_id,
            "status": "SUCCEEDED",
            "steps": [{"result": {"status": "SUCCEEDED"}}],
            "artifacts": [{"name": "execution.ipynb"}],
            "notebook_path": "users/u/executions/e/notebooks/execution.ipynb",
            "notebook": {
                "cells": [
                    {"outputs": [{"output_type": "stream", "name": "stdout", "text": "55\n"}]}
                ]
            },
        }

    class FakeWaiter:
        def __init__(self, *_args, **kwargs):
            assert kwargs["include_existing"] is True

        async def open(self):
            pass

        async def close(self):
            pass

        async def wait_for_terminal(self, requested_execution_id, *, timeout_seconds, event_types):
            assert requested_execution_id == execution_id
            assert timeout_seconds > 0
            assert event_types == {"execution.succeeded", "execution.failed"}
            return graph_module.ExecutionEventEnvelope.model_validate(terminal_event)

    monkeypatch.setattr(graph_module, "_chat_model", lambda: object())
    monkeypatch.setattr(graph_module, "load_executor_tools", fake_load_tools)
    monkeypatch.setattr(graph_module, "create_agent", fake_create_agent)
    monkeypatch.setattr(graph_module, "reconcile_execution", fake_reconcile_execution)
    monkeypatch.setattr(graph_module, "ExecutionEventWaiter", FakeWaiter)

    raw_result = await graph.ainvoke(
        AgentState(messages=[HumanMessage(content="1부터 10까지 합계를 실행해줘")]),
        RunnableConfig(configurable={"thread_id": "chat-test"}),
    )
    result = AgentState.model_validate(raw_result)

    assert result.phase == "SUCCEEDED"
    assert result.execution_id == execution_id
    assert result.execution_request is None
    assert "55" in str(result.messages[-1].content)
