"""Minimal graph served by ``langgraph dev`` before Executor integration is added."""

from functools import lru_cache
from typing import Any

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from executor_test_agent.config import get_settings
from executor_test_agent.state import AgentState

BOOTSTRAP_MESSAGE = (
    "Executor Test Agent is ready. Executor MCP and Redis event integration will be added in the "
    "next stage."
)


@lru_cache(maxsize=1)
def _chat_model() -> ChatOpenAI | None:
    settings = get_settings()
    if settings.llm_model is None:
        return None
    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=0,
    )


async def respond(state: AgentState) -> dict[str, Any]:
    """Return a local bootstrap response or invoke the configured vLLM model."""
    model = _chat_model()
    if model is None:
        message = AIMessage(content=BOOTSTRAP_MESSAGE)
    else:
        message = await model.ainvoke(state.messages)
    return {"messages": [message], "phase": "READY"}


builder = StateGraph(AgentState)
builder.add_node("respond", respond)
builder.add_edge(START, "respond")
builder.add_edge("respond", END)

graph = builder.compile()
