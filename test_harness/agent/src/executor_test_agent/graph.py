"""LangGraph test Agent for checkpointed Executor integration exercises."""

from functools import lru_cache
from typing import Any

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from executor_test_agent.config import get_settings
from executor_test_agent.integrations.contracts import (
    AgentExecutionRequest,
    ExecutionEventEnvelope,
)
from executor_test_agent.integrations.workflow import reconcile_execution, submit_execution
from executor_test_agent.state import AgentState

BOOTSTRAP_MESSAGE = "Executor Test Agent is ready."


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
    """Route deterministic execution requests before optional free-form LLM responses."""
    if state.execution_request is not None:
        return {"phase": "SUBMITTING"}
    model = _chat_model()
    message = (
        AIMessage(content=BOOTSTRAP_MESSAGE)
        if model is None
        else await model.ainvoke(state.messages)
    )
    return {"messages": [message], "phase": "READY"}


async def submit(state: AgentState) -> dict[str, Any]:
    """Submit without waiting so the graph can checkpoint before long-running work."""
    if state.execution_request is None:
        raise ValueError("execution_request is required in the execution branch.")
    request = AgentExecutionRequest.model_validate(state.execution_request)
    try:
        execution_id = await submit_execution(request, get_settings())
    except Exception as exc:
        return {
            "messages": [AIMessage(content=f"Executor submission failed: {type(exc).__name__}")],
            "phase": "FAILED",
        }
    return {"phase": "WAITING_FOR_EVENT", "execution_id": execution_id}


def wait_for_event(state: AgentState) -> dict[str, Any]:
    """Suspend at a checkpoint until the event bridge supplies a terminal event."""
    if state.execution_id is None:
        raise ValueError("execution_id is required before waiting for an event.")
    resumed = interrupt({"execution_id": state.execution_id})
    event = ExecutionEventEnvelope.model_validate(resumed)
    return {"phase": "VERIFYING", "terminal_event": event.model_dump(mode="json")}


async def verify(state: AgentState) -> dict[str, Any]:
    """Reconcile PostgreSQL-backed Executor state and Runtime-owned Jupyter output."""
    if state.execution_id is None or state.terminal_event is None:
        raise ValueError("execution_id and terminal_event are required for verification.")
    terminal_event = ExecutionEventEnvelope.model_validate(state.terminal_event)
    try:
        result = await reconcile_execution(state.execution_id, terminal_event, get_settings())
    except Exception as exc:
        return {
            "messages": [AIMessage(content=f"Executor verification failed: {type(exc).__name__}")],
            "phase": "FAILED",
        }

    artifact_names = sorted(artifact["name"] for artifact in result["artifacts"])
    message = AIMessage(
        content=(
            f"Execution {result['execution_id']} {result['status']}. "
            f"Steps={len(result['steps'])}, artifacts={artifact_names}, "
            f"notebook={result['notebook_path']}"
        )
    )
    return {
        "messages": [message],
        "phase": "SUCCEEDED" if result["status"] == "SUCCEEDED" else "FAILED",
        "execution_id": result["execution_id"],
        "execution_result": result,
    }


def _route_after_respond(state: AgentState) -> str:
    return "submit" if state.phase == "SUBMITTING" else END


def _route_after_submit(state: AgentState) -> str:
    return END if state.phase == "FAILED" else "wait_for_event"


builder = StateGraph(AgentState)
builder.add_node("respond", respond)
builder.add_node("submit", submit)
builder.add_node("wait_for_event", wait_for_event)
builder.add_node("verify", verify)
builder.add_edge(START, "respond")
builder.add_conditional_edges("respond", _route_after_respond, {"submit": "submit", END: END})
builder.add_conditional_edges(
    "submit", _route_after_submit, {"wait_for_event": "wait_for_event", END: END}
)
builder.add_edge("wait_for_event", "verify")
builder.add_edge("verify", END)

graph = builder.compile()
