"""Local graph contract tests that never call an external LLM."""

from langchain_core.messages import AIMessage, HumanMessage
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
