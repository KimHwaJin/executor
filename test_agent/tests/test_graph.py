"""Local graph contract tests that never call an external LLM."""

from langchain_core.messages import AIMessage, HumanMessage

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
