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


def _event(
    execution_id: str,
    event_id: str,
    event_type: str,
    status: str,
    **payload,
) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "schema_version": "1.0",
        "execution_id": execution_id,
        "occurred_at": "2026-08-13T00:00:00Z",
        "payload": {
            "status": status,
            **(
                {"execution_status": status}
                if event_type == "execution.operation_completed"
                else {}
            ),
            **payload,
        },
    }


def _batch(*events: dict) -> dict:
    ordered = [
        {**event, "event_sequence": sequence} for sequence, event in enumerate(events, start=1)
    ]
    return {"events": ordered, "wake_event": ordered[-1]}


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
        return {
            "execution_id": "00000000-0000-0000-0000-000000000001",
            "operation": {
                "operation_id": "00000000-0000-0000-0000-000000000003",
                "steps": [],
            },
            "state": {"status": "QUEUED", "version": 0},
            "event_stream_start_id": "10-0",
        }

    async def fake_reconcile_execution(execution_id, event_batch, _settings):
        assert execution_id == "00000000-0000-0000-0000-000000000001"
        assert event_batch.wake_event.event_type == "execution.completed"
        return {
            "execution_id": execution_id,
            "status": "SUCCEEDED",
            "steps": [
                {
                    "result": {
                        "status": "SUCCEEDED",
                        "resolved_outputs": [
                            {
                                "kind": "STREAM",
                                "representations": [
                                    {
                                        "media_type": "text/plain",
                                        "content": "55\n",
                                    }
                                ],
                            }
                        ],
                        "error_message": None,
                    }
                }
            ],
            "artifacts": [{"name": "result.txt"}],
            "notebook_path": "users/u/executions/execution-1/notebooks/execution.ipynb",
            "notebook": {
                "cells": [{"outputs": [{"output_type": "stream", "name": "stdout", "text": "3\n"}]}]
            },
            "step_events": [],
            "operation_events": [],
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
    terminal_event = _event(
        execution_id,
        "00000000-0000-0000-0000-000000000002",
        "execution.completed",
        "SUCCEEDED",
    )
    raw_result = await checkpointed_graph.ainvoke(Command(resume=_batch(terminal_event)), config)
    result = AgentState.model_validate(raw_result)

    assert result.phase == "SUCCEEDED"
    assert result.execution_id == execution_id
    assert result.execution_result is not None
    assert "result.txt" in str(result.messages[-1].content)


async def test_graph_runs_mcp_tool_agent_and_waits_for_stream_event(monkeypatch) -> None:
    execution_id = "00000000-0000-0000-0000-000000000010"
    terminal_event = _event(
        execution_id,
        "00000000-0000-0000-0000-000000000011",
        "execution.completed",
        "SUCCEEDED",
    )

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
                "event_types": ["execution.completed"],
                "event_stream_start_id": "20-0",
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

    async def fake_reconcile_execution(_execution_id, _event_batch, _settings):
        return {
            "execution_id": execution_id,
            "status": "SUCCEEDED",
            "steps": [
                {
                    "result": {
                        "status": "SUCCEEDED",
                        "resolved_outputs": [
                            {
                                "kind": "STREAM",
                                "representations": [
                                    {
                                        "media_type": "text/plain",
                                        "content": "55\n",
                                    }
                                ],
                            }
                        ],
                        "error_message": None,
                    }
                }
            ],
            "artifacts": [{"name": "execution.ipynb"}],
            "notebook_path": "users/u/executions/e/notebooks/execution.ipynb",
            "notebook": {
                "cells": [
                    {"outputs": [{"output_type": "stream", "name": "stdout", "text": "55\n"}]}
                ]
            },
            "step_events": [],
            "operation_events": [],
        }

    class FakeWaiter:
        def __init__(self, *_args, **kwargs):
            assert kwargs["start_id"] == "20-0"

        async def open(self):
            pass

        async def close(self):
            pass

        async def wait_for_wakeup(
            self,
            requested_execution_id,
            *,
            timeout_seconds,
            event_types,
            operation_id,
            after_sequence,
        ):
            assert requested_execution_id == execution_id
            assert timeout_seconds > 0
            assert event_types == {"execution.completed"}
            assert operation_id is None
            assert after_sequence == 0
            return graph_module.ExecutionEventBatch.model_validate(_batch(terminal_event))

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


async def test_graph_advances_and_finalizes_deterministic_multi_scenario(monkeypatch) -> None:
    execution_id = "00000000-0000-0000-0000-000000000030"
    calls: list[str] = []

    async def fake_submit_execution(request, _settings):
        assert request.operation_mode == "MULTI"
        calls.append("submit")
        return {
            "execution_id": execution_id,
            "operation": {
                "operation_id": "00000000-0000-0000-0000-000000000031",
                "steps": [{"sequence": 0, "step_id": "step-0"}],
            },
            "state": {"status": "QUEUED", "version": 0},
            "event_stream_start_id": "30-0",
        }

    async def fake_create_operation(
        requested_execution_id,
        steps,
        _settings,
        *,
        operation_index,
        expected_version,
        first_sequence,
    ):
        assert requested_execution_id == execution_id
        assert operation_index == 0
        assert expected_version == 2
        assert first_sequence == 1
        assert steps[0].tool_name == "double_value"
        calls.append("operation")
        return {
            "execution_id": execution_id,
            "operation": {
                "operation_id": "00000000-0000-0000-0000-000000000032",
                "steps": [{"sequence": 1, "step_id": "step-1"}],
            },
            "state": {"status": "QUEUED", "version": 3},
            "event_stream_start_id": "31-0",
        }

    async def fake_finalize(requested_execution_id, _settings, *, expected_version):
        assert requested_execution_id == execution_id
        assert expected_version == 5
        calls.append("finalize")
        return {
            "execution_id": execution_id,
            "operation": None,
            "state": {"status": "FINALIZING", "version": 6},
            "event_stream_start_id": "32-0",
        }

    async def fake_reconcile(requested_execution_id, event_batch, _settings):
        assert requested_execution_id == execution_id
        status = event_batch.wake_event.payload["status"]
        version = 2 if calls == ["submit"] else 5
        return {
            "execution_id": execution_id,
            "status": status,
            "version": version,
            "steps": [{"sequence": index} for index in range(len(calls))],
            "artifacts": ([{"name": "execution.ipynb"}] if status == "SUCCEEDED" else []),
            "notebook_path": "users/u/executions/e/notebooks/execution.ipynb",
            "notebook": {"cells": []},
            "step_events": [
                event.model_dump(mode="json")
                for event in event_batch.events
                if event.event_type == "execution.step_completed"
            ],
            "operation_events": [],
            "wake_event_id": str(event_batch.wake_event.event_id),
            "wake_event_type": event_batch.wake_event.event_type,
        }

    monkeypatch.setattr(graph_module, "submit_execution", fake_submit_execution)
    monkeypatch.setattr(graph_module, "create_execution_operation", fake_create_operation)
    monkeypatch.setattr(graph_module, "finalize_execution", fake_finalize)
    monkeypatch.setattr(graph_module, "reconcile_execution", fake_reconcile)

    checkpointed_graph = graph_module.builder.compile(checkpointer=MemorySaver())
    config = RunnableConfig(configurable={"thread_id": "multi-graph-test"})
    state = await checkpointed_graph.ainvoke(
        AgentState(
            messages=[HumanMessage(content="Run a MULTI scenario")],
            execution_request={
                "runtime_profile": "basic",
                "user_id": "u",
                "project_id": "p",
                "session_id": "s",
                "task_id": "t",
                "operation_mode": "MULTI",
                "steps": [{"skill_name": "eda", "tool_name": "make_value", "code": "value = 3"}],
                "follow_up_operations": [
                    [
                        {
                            "skill_name": "eda",
                            "tool_name": "double_value",
                            "code": "value = value * 2\nprint(value)",
                        }
                    ]
                ],
            },
        ),
        config,
    )
    assert state["phase"] == "WAITING_FOR_EVENT"
    assert state["awaited_operation_id"] == "00000000-0000-0000-0000-000000000031"

    first_step = _event(
        execution_id,
        "00000000-0000-0000-0000-000000000033",
        "execution.step_completed",
        "SUCCEEDED",
        result={"outputs": [], "execution_count": 1},
    )
    first_wait = _event(
        execution_id,
        "00000000-0000-0000-0000-000000000034",
        "execution.operation_completed",
        "WAITING_FOR_OPERATION",
    )
    state = await checkpointed_graph.ainvoke(Command(resume=_batch(first_step, first_wait)), config)
    assert state["phase"] == "WAITING_FOR_EVENT"
    assert state["next_operation_index"] == 1
    assert state["awaited_operation_id"] == "00000000-0000-0000-0000-000000000032"

    second_step = _event(
        execution_id,
        "00000000-0000-0000-0000-000000000035",
        "execution.step_completed",
        "SUCCEEDED",
        result={"outputs": [], "execution_count": 2},
    )
    second_wait = _event(
        execution_id,
        "00000000-0000-0000-0000-000000000036",
        "execution.operation_completed",
        "WAITING_FOR_OPERATION",
    )
    state = await checkpointed_graph.ainvoke(
        Command(resume=_batch(second_step, second_wait)), config
    )
    assert state["phase"] == "WAITING_FOR_EVENT"
    assert state["awaited_operation_id"] is None

    succeeded = _event(
        execution_id,
        "00000000-0000-0000-0000-000000000037",
        "execution.completed",
        "SUCCEEDED",
    )
    state = await checkpointed_graph.ainvoke(Command(resume=_batch(succeeded)), config)
    result = AgentState.model_validate(state)

    assert result.phase == "SUCCEEDED"
    assert calls == ["submit", "operation", "finalize"]
    assert len(result.command_receipts) == 3
    assert len(result.event_history) == 5
    assert result.execution_result is not None
    assert result.execution_result["wake_event_type"] == "execution.completed"


async def test_graph_ends_cleanly_when_stream_event_wait_fails(monkeypatch) -> None:
    execution_id = "00000000-0000-0000-0000-000000000020"

    async def fake_load_tools(_settings, *, request_scope_id):
        assert request_scope_id.startswith("chat-failure-")
        return [object()]

    class FakeToolAgent:
        async def ainvoke(self, state):
            mutation = {
                "execution_id": execution_id,
                "status": "QUEUED",
                "wait_for_event": True,
                "event_types": ["execution.completed"],
                "event_stream_start_id": "40-0",
            }
            return {
                "messages": [
                    *state["messages"],
                    ToolMessage(
                        content=json.dumps(mutation),
                        tool_call_id="call-failure",
                        name="execution_submit",
                    ),
                ]
            }

    class FailingWaiter:
        def __init__(self, *_args, **kwargs):
            assert kwargs["start_id"] == "40-0"

        async def open(self):
            pass

        async def close(self):
            pass

        async def wait_for_wakeup(self, *_args, **_kwargs):
            raise TimeoutError

    async def unexpected_reconcile(*_args, **_kwargs):
        raise AssertionError("verify must not run without a terminal event")

    monkeypatch.setattr(graph_module, "_chat_model", lambda: object())
    monkeypatch.setattr(graph_module, "load_executor_tools", fake_load_tools)
    monkeypatch.setattr(
        graph_module,
        "create_agent",
        lambda _model, _tools, *, system_prompt: FakeToolAgent(),
    )
    monkeypatch.setattr(graph_module, "ExecutionEventWaiter", FailingWaiter)
    monkeypatch.setattr(graph_module, "reconcile_execution", unexpected_reconcile)

    raw_result = await graph.ainvoke(
        AgentState(messages=[HumanMessage(content="실패 경로를 확인해줘")]),
        RunnableConfig(configurable={"thread_id": "chat-failure"}),
    )
    result = AgentState.model_validate(raw_result)

    assert result.phase == "FAILED"
    assert result.execution_id == execution_id
    assert result.event_batch is None
    assert "Executor event wait failed: TimeoutError" in str(result.messages[-1].content)
