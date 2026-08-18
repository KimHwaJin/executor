"""LangGraph test Agent for checkpointed Executor integration exercises."""

import json
from functools import lru_cache
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, ToolMessage
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
from executor_test_agent.mcp_tools import (
    MCP_AGENT_SYSTEM_PROMPT,
    MUTATION_MCP_TOOL_NAMES,
    load_executor_tools,
)
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
    """Route explicit requests or run a Tool-calling Agent over approved Executor MCP Tools."""
    if state.execution_request is not None:
        return {"phase": "SUBMITTING"}
    model = _chat_model()
    settings = get_settings()
    if model is None:
        return {"messages": [AIMessage(content=BOOTSTRAP_MESSAGE)], "phase": "READY"}
    if not settings.natural_language_execution_enabled:
        return {"messages": [await model.ainvoke(state.messages)], "phase": "READY"}

    try:
        thread_id = str(config.get("configurable", {}).get("thread_id") or "unscoped")
        message_id = getattr(state.messages[-1], "id", None) if state.messages else None
        request_scope_id = f"{thread_id}-{message_id or len(state.messages)}"
        tools = await load_executor_tools(settings, request_scope_id=request_scope_id)
        tool_agent = create_agent(model, tools, system_prompt=MCP_AGENT_SYSTEM_PROMPT)
        result = await tool_agent.ainvoke({"messages": state.messages})
    except Exception as exc:
        return {
            "messages": [AIMessage(content=f"Executor Tool Agent failed: {type(exc).__name__}")],
            "phase": "FAILED",
        }
    result_messages = result.get("messages", [])
    new_messages = list(result_messages[len(state.messages) :])
    mutation = _last_mutation_result(new_messages)
    if mutation is None:
        return {"messages": new_messages, "phase": "READY"}
    return {
        "messages": new_messages,
        "execution_id": mutation["execution_id"],
        "wait_strategy": "STREAM",
        "awaited_event_types": mutation["event_types"],
        "phase": "WAITING_FOR_EVENT",
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
                event_types=set(state.awaited_event_types) or None,
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
        "phase": _phase_for_execution_status(result["status"]),
        "execution_id": result["execution_id"],
        "execution_result": result,
        "execution_request": None,
        "terminal_event": None,
        "wait_strategy": "INTERRUPT",
        "awaited_event_types": [],
    }


def _route_after_respond(state: AgentState) -> str:
    if state.phase == "SUBMITTING":
        return "submit"
    if state.phase == "WAITING_FOR_EVENT":
        return "wait_for_stream_event"
    return END


def _route_after_submit(state: AgentState) -> str:
    if state.phase == "FAILED":
        return END
    return "wait_for_stream_event" if state.wait_strategy == "STREAM" else "wait_for_event"


def _route_after_event_wait(state: AgentState) -> str:
    """Verify only after a terminal event was successfully materialized."""
    return "verify" if state.phase == "VERIFYING" else END


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


def _last_mutation_result(messages: list[Any]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if not isinstance(message, ToolMessage) or message.name not in MUTATION_MCP_TOOL_NAMES:
            continue
        content = message.content
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(payload, dict)
            and payload.get("wait_for_event") is True
            and isinstance(payload.get("execution_id"), str)
            and isinstance(payload.get("event_types"), list)
        ):
            return payload
    return None


def _phase_for_execution_status(status: str) -> str:
    if status == "SUCCEEDED":
        return "SUCCEEDED"
    if status == "WAITING_FOR_OPERATION":
        return "READY"
    return "FAILED"


builder = StateGraph(AgentState)
builder.add_node("respond", respond)
builder.add_node("submit", submit)
builder.add_node("wait_for_event", wait_for_event)
builder.add_node("wait_for_stream_event", wait_for_stream_event)
builder.add_node("verify", verify)
builder.add_edge(START, "respond")
builder.add_conditional_edges(
    "respond",
    _route_after_respond,
    {"submit": "submit", "wait_for_stream_event": "wait_for_stream_event", END: END},
)
builder.add_conditional_edges(
    "submit",
    _route_after_submit,
    {
        "wait_for_event": "wait_for_event",
        "wait_for_stream_event": "wait_for_stream_event",
        END: END,
    },
)
builder.add_conditional_edges(
    "wait_for_event",
    _route_after_event_wait,
    {"verify": "verify", END: END},
)
builder.add_conditional_edges(
    "wait_for_stream_event",
    _route_after_event_wait,
    {"verify": "verify", END: END},
)
builder.add_edge("verify", END)

graph = builder.compile()
