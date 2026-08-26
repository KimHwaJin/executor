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
    ExecutionEventBatch,
)
from executor_test_agent.integrations.errors import exception_summary
from executor_test_agent.integrations.events import (
    MULTI_OPERATION_WAKE_EVENT_TYPES,
    TERMINAL_EVENT_TYPES,
    ExecutionEventWaiter,
)
from executor_test_agent.integrations.workflow import (
    create_execution_operation,
    finalize_execution,
    reconcile_execution,
    submit_execution,
)
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
        "awaited_operation_id": _operation_id(mutation),
        "event_stream_start_id": _stream_start_id(mutation),
        "phase": "WAITING_FOR_EVENT",
    }


async def submit(state: AgentState) -> dict[str, Any]:
    """Submit without waiting so the graph can checkpoint before long-running work."""
    if state.execution_request is None:
        raise ValueError("execution_request is required in the execution branch.")
    request = AgentExecutionRequest.model_validate(state.execution_request)
    try:
        receipt = await submit_execution(request, get_settings())
    except Exception as exc:
        return {
            "messages": [AIMessage(content=f"Executor submission failed: {type(exc).__name__}")],
            "phase": "FAILED",
            "execution_request": None,
            "wait_strategy": "INTERRUPT",
        }
    return {
        "phase": "WAITING_FOR_EVENT",
        "execution_id": str(receipt["execution_id"]),
        "awaited_event_types": sorted(_wake_event_types(request.operation_mode)),
        "awaited_operation_id": _operation_id(receipt),
        "event_stream_start_id": _stream_start_id(receipt),
        "command_receipts": [*state.command_receipts, receipt],
        "next_operation_index": 0,
    }


def wait_for_event(state: AgentState) -> dict[str, Any]:
    """Suspend until the external event bridge supplies a validated event batch."""

    if state.execution_id is None:
        raise ValueError("execution_id is required before waiting for an event.")
    resumed = interrupt(
        {
            "execution_id": state.execution_id,
            "event_types": state.awaited_event_types,
            "operation_id": state.awaited_operation_id,
        }
    )
    batch = ExecutionEventBatch.model_validate(resumed)
    return {"phase": "VERIFYING", "event_batch": batch.model_dump(mode="json")}


async def wait_for_stream_event(state: AgentState) -> dict[str, Any]:
    """Keep a Chat UI run alive until its requested Executor boundary is observable."""
    if state.execution_id is None:
        raise ValueError("execution_id is required before waiting for an event.")
    if state.event_stream_start_id is None:
        raise ValueError("event_stream_start_id is required for in-process event waiting.")
    settings = get_settings()
    try:
        waiter = ExecutionEventWaiter(
            settings.executor_redis_url,
            settings.executor_event_stream,
            settings.executor_consumer_group_prefix,
            executor_mcp_url=settings.executor_mcp_url,
            start_id=state.event_stream_start_id,
        )
        await waiter.open()
        try:
            batch = await waiter.wait_for_wakeup(
                state.execution_id,
                timeout_seconds=settings.execution_timeout_seconds,
                event_types=set(state.awaited_event_types) or None,
                operation_id=state.awaited_operation_id,
                after_sequence=state.last_event_sequence,
            )
        finally:
            await waiter.close()
    except Exception as exc:
        return {
            "messages": [AIMessage(content=f"Executor event wait failed: {type(exc).__name__}")],
            "phase": "FAILED",
            "execution_request": None,
            "event_batch": None,
            "wait_strategy": "INTERRUPT",
        }
    return {"phase": "VERIFYING", "event_batch": batch.model_dump(mode="json")}


async def verify(state: AgentState) -> dict[str, Any]:
    """Reconcile PostgreSQL-backed Executor state and Runtime-owned Jupyter output."""

    if state.execution_id is None or state.event_batch is None:
        raise ValueError("execution_id and event_batch are required for verification.")
    event_batch = ExecutionEventBatch.model_validate(state.event_batch)
    try:
        result = await reconcile_execution(state.execution_id, event_batch, get_settings())
    except Exception as exc:
        return {
            "messages": [
                AIMessage(content=(f"Executor verification failed: {exception_summary(exc)}"))
            ],
            "phase": "FAILED",
            "execution_request": None,
            "event_batch": None,
            "wait_strategy": "INTERRUPT",
        }

    artifact_names = sorted(artifact["name"] for artifact in result["artifacts"])
    output_text = _step_output_text(result["steps"])
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
    phase = _phase_for_execution_status(result["status"])
    scenario_request = (
        AgentExecutionRequest.model_validate(state.execution_request)
        if state.execution_request is not None
        else None
    )
    if result["status"] == "WAITING_FOR_OPERATION" and scenario_request is not None:
        has_follow_up = state.next_operation_index < len(scenario_request.follow_up_operations)
        if has_follow_up or scenario_request.auto_finalize:
            phase = "ADVANCING"
    return {
        "messages": [message],
        "phase": phase,
        "execution_id": result["execution_id"],
        "execution_result": result,
        "execution_request": (state.execution_request if phase == "ADVANCING" else None),
        "event_batch": None,
        "event_history": [
            *state.event_history,
            *(event.model_dump(mode="json") for event in event_batch.events),
        ],
        "last_event_sequence": event_batch.wake_event.event_sequence,
        "awaited_event_types": [],
        "awaited_operation_id": None,
        "event_stream_start_id": None,
    }


async def advance_multi(state: AgentState) -> dict[str, Any]:
    """Submit the next deterministic Operation or finalize the MULTI Execution."""

    if (
        state.execution_id is None
        or state.execution_request is None
        or state.execution_result is None
    ):
        raise ValueError("A deterministic MULTI scenario is required before advancing.")
    request = AgentExecutionRequest.model_validate(state.execution_request)
    expected_version = int(state.execution_result["version"])
    try:
        if state.next_operation_index < len(request.follow_up_operations):
            operation_index = state.next_operation_index
            first_sequence = (
                max(
                    (int(step["sequence"]) for step in state.execution_result["steps"]),
                    default=-1,
                )
                + 1
            )
            receipt = await create_execution_operation(
                state.execution_id,
                request.follow_up_operations[operation_index],
                get_settings(),
                operation_index=operation_index,
                expected_version=expected_version,
                first_sequence=first_sequence,
            )
            return {
                "phase": "WAITING_FOR_EVENT",
                "next_operation_index": operation_index + 1,
                "awaited_event_types": sorted(MULTI_OPERATION_WAKE_EVENT_TYPES),
                "awaited_operation_id": _operation_id(receipt),
                "event_stream_start_id": _stream_start_id(receipt),
                "command_receipts": [*state.command_receipts, receipt],
            }
        if request.auto_finalize:
            receipt = await finalize_execution(
                state.execution_id,
                get_settings(),
                expected_version=expected_version,
            )
            return {
                "phase": "WAITING_FOR_EVENT",
                "awaited_event_types": sorted(TERMINAL_EVENT_TYPES),
                "awaited_operation_id": None,
                "event_stream_start_id": _stream_start_id(receipt),
                "command_receipts": [*state.command_receipts, receipt],
            }
    except Exception as exc:
        return {
            "messages": [AIMessage(content=f"Executor MULTI advance failed: {type(exc).__name__}")],
            "phase": "FAILED",
            "execution_request": None,
            "event_batch": None,
        }
    return {"phase": "READY", "execution_request": None}


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
    """Verify only after a requested event batch was materialized."""

    return "verify" if state.phase == "VERIFYING" else END


def _route_after_verify(state: AgentState) -> str:
    return "advance_multi" if state.phase == "ADVANCING" else END


def _route_after_advance(state: AgentState) -> str:
    if state.phase == "WAITING_FOR_EVENT":
        return "wait_for_stream_event" if state.wait_strategy == "STREAM" else "wait_for_event"
    return END


def _step_output_text(steps: list[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for step in steps:
        result = step.get("result", {})
        error = result.get("error_message")
        if error:
            rendered.append(str(error))
        for output in result.get("resolved_outputs", []):
            for representation in output.get("representations", []):
                content = representation.get("content")
                if isinstance(content, str) and content.strip():
                    rendered.append(content.strip())
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


def _wake_event_types(operation_mode: str) -> set[str]:
    return MULTI_OPERATION_WAKE_EVENT_TYPES if operation_mode == "MULTI" else TERMINAL_EVENT_TYPES


def _operation_id(receipt: dict[str, Any]) -> str | None:
    operation = receipt.get("operation")
    if not isinstance(operation, dict):
        return None
    operation_id = operation.get("operation_id")
    return str(operation_id) if operation_id is not None else None


def _stream_start_id(receipt: dict[str, Any]) -> str:
    start_id = receipt.get("event_stream_start_id")
    if not isinstance(start_id, str) or not start_id:
        raise ValueError("Mutation receipt is missing event_stream_start_id.")
    return start_id


builder = StateGraph(AgentState)
builder.add_node("respond", respond)
builder.add_node("submit", submit)
builder.add_node("wait_for_event", wait_for_event)
builder.add_node("wait_for_stream_event", wait_for_stream_event)
builder.add_node("verify", verify)
builder.add_node("advance_multi", advance_multi)
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
builder.add_conditional_edges(
    "verify",
    _route_after_verify,
    {"advance_multi": "advance_multi", END: END},
)
builder.add_conditional_edges(
    "advance_multi",
    _route_after_advance,
    {
        "wait_for_event": "wait_for_event",
        "wait_for_stream_event": "wait_for_stream_event",
        END: END,
    },
)

graph = builder.compile()
