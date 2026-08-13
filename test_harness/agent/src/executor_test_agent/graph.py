"""LangGraph test Agent for checkpointed Executor integration exercises."""

from functools import lru_cache
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from executor_test_agent.config import get_settings
from executor_test_agent.integrations.contracts import (
    AgentExecutionRequest,
    ExecutionEventEnvelope,
)
from executor_test_agent.integrations.events import ExecutionEventWaiter
from executor_test_agent.integrations.workflow import reconcile_execution, submit_execution
from executor_test_agent.planning import decision_steps, plan_message
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


async def respond(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Route explicit requests or turn a natural-language message into chat or execution."""
    if state.execution_request is not None:
        return {"phase": "SUBMITTING"}
    model = _chat_model()
    settings = get_settings()
    if model is None:
        return {"messages": [AIMessage(content=BOOTSTRAP_MESSAGE)], "phase": "READY"}
    if not settings.natural_language_execution_enabled:
        return {"messages": [await model.ainvoke(state.messages)], "phase": "READY"}

    try:
        decision = await plan_message(model, state.messages)
    except Exception as exc:
        return {
            "messages": [AIMessage(content=f"Execution planning failed: {type(exc).__name__}")],
            "phase": "FAILED",
        }
    if decision.intent == "CHAT":
        return {"messages": [AIMessage(content=decision.response)], "phase": "READY"}

    unique = uuid4().hex
    thread_id = str(config.get("configurable", {}).get("thread_id") or unique)
    request = {
        "runtime_profile": decision.runtime_profile,
        "user_id": settings.default_user_id,
        "project_id": settings.default_project_id,
        "session_id": f"chat-{thread_id}",
        "task_id": f"chat-task-{unique}",
        "execution_plan_id": f"chat-plan-{unique}",
        "steps": decision_steps(decision),
    }
    summary = decision.response.strip() or f"Executing {len(decision.steps)} planned Step(s)."
    return {
        "messages": [AIMessage(content=summary)],
        "execution_request": request,
        "wait_strategy": "STREAM",
        "phase": "SUBMITTING",
    }


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
            "execution_request": None,
            "wait_strategy": "INTERRUPT",
        }
    return {"phase": "WAITING_FOR_EVENT", "execution_id": execution_id}


def wait_for_event(state: AgentState) -> dict[str, Any]:
    """Suspend at a checkpoint until the event bridge supplies a terminal event."""
    if state.execution_id is None:
        raise ValueError("execution_id is required before waiting for an event.")
    resumed = interrupt({"execution_id": state.execution_id})
    event = ExecutionEventEnvelope.model_validate(resumed)
    return {"phase": "VERIFYING", "terminal_event": event.model_dump(mode="json")}


async def wait_for_stream_event(state: AgentState) -> dict[str, Any]:
    """Keep a Chat UI run alive until its Executor terminal event is observable."""
    if state.execution_id is None:
        raise ValueError("execution_id is required before waiting for an event.")
    settings = get_settings()
    try:
        waiter = ExecutionEventWaiter(
            settings.executor_redis_url,
            settings.executor_event_stream,
            settings.executor_consumer_group_prefix,
            include_existing=True,
        )
        await waiter.open()
        try:
            event = await waiter.wait_for_terminal(
                state.execution_id,
                timeout_seconds=settings.execution_timeout_seconds,
            )
        finally:
            await waiter.close()
    except Exception as exc:
        return {
            "messages": [AIMessage(content=f"Executor event wait failed: {type(exc).__name__}")],
            "phase": "FAILED",
            "execution_request": None,
            "wait_strategy": "INTERRUPT",
        }
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
            "execution_request": None,
            "terminal_event": None,
            "wait_strategy": "INTERRUPT",
        }

    artifact_names = sorted(artifact["name"] for artifact in result["artifacts"])
    output_text = _notebook_output_text(result["notebook"])
    message_lines = [
        f"Execution {result['execution_id']} {result['status']}.",
        f"Steps: {len(result['steps'])}",
    ]
    if output_text:
        message_lines.extend(["Output:", output_text])
    message_lines.extend(
        [
            f"Artifacts: {artifact_names}",
            f"Notebook: {result['notebook_path']}",
        ]
    )
    message = AIMessage(content="\n\n".join(message_lines))
    return {
        "messages": [message],
        "phase": "SUCCEEDED" if result["status"] == "SUCCEEDED" else "FAILED",
        "execution_id": result["execution_id"],
        "execution_result": result,
        "execution_request": None,
        "terminal_event": None,
        "wait_strategy": "INTERRUPT",
    }


def _route_after_respond(state: AgentState) -> str:
    return "submit" if state.phase == "SUBMITTING" else END


def _route_after_submit(state: AgentState) -> str:
    if state.phase == "FAILED":
        return END
    return "wait_for_stream_event" if state.wait_strategy == "STREAM" else "wait_for_event"


def _notebook_output_text(notebook: dict[str, Any]) -> str:
    rendered: list[str] = []
    for cell in notebook.get("cells", []):
        for output in cell.get("outputs", []):
            text = output.get("text")
            if isinstance(text, list):
                rendered.append("".join(str(part) for part in text).strip())
            elif isinstance(text, str):
                rendered.append(text.strip())
            data = output.get("data")
            if isinstance(data, dict):
                plain = data.get("text/plain")
                if isinstance(plain, list):
                    rendered.append("".join(str(part) for part in plain).strip())
                elif isinstance(plain, str):
                    rendered.append(plain.strip())
    return "\n".join(part for part in rendered if part)


builder = StateGraph(AgentState)
builder.add_node("respond", respond)
builder.add_node("submit", submit)
builder.add_node("wait_for_event", wait_for_event)
builder.add_node("wait_for_stream_event", wait_for_stream_event)
builder.add_node("verify", verify)
builder.add_edge(START, "respond")
builder.add_conditional_edges("respond", _route_after_respond, {"submit": "submit", END: END})
builder.add_conditional_edges(
    "submit",
    _route_after_submit,
    {
        "wait_for_event": "wait_for_event",
        "wait_for_stream_event": "wait_for_stream_event",
        END: END,
    },
)
builder.add_edge("wait_for_event", "verify")
builder.add_edge("wait_for_stream_event", "verify")
builder.add_edge("verify", END)

graph = builder.compile()
