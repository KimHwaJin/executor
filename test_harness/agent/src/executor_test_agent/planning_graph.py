"""Guarded conversation -> plan -> HITL -> Executor workflow for Agent Chat UI."""

import hashlib
from functools import lru_cache
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
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
from executor_test_agent.mcp_tools import load_executor_read_tools
from executor_test_agent.planning import (
    ExecutionPlan,
    RequestRoute,
    parse_plan_review,
    plan_review_interrupt,
    render_plan,
)
from executor_test_agent.planning_state import PlanningAgentState

PLANNING_AGENT_ID = "executor-planning-agent"

ROUTER_PROMPT = """
Classify the latest user request. Use EXECUTION only when the user asks to run, calculate,
analyze, transform, train, evaluate, plot, or otherwise execute Python code. Use CHAT for ordinary
conversation, explanations, and read-only questions. Do not execute code during classification.
""".strip()

READ_ONLY_PROMPT = """
You are a helpful conversational Agent. Answer ordinary questions directly. You may use the
provided read-only Executor MCP Tools for current Runtime or Execution facts. You have no mutation
Tools and must never claim that you submitted, changed, or cancelled an Execution.
""".strip()

PLANNER_PROMPT = """
Create a complete, user-reviewable Python execution plan for the latest request.

Rules:
- Use runtime_profile basic unless the request clearly requires the ml profile.
- Use SINGLE when every Step can be planned now and run as one Operation.
- Use MULTI only when later Operations must reuse the retained Runtime state or are intentionally
  separated into observable boundaries. Put every already-known Operation in order.
- Each Step is one valid Python cell. Keep code deterministic and concise.
- Do not use network, process, environment, dynamic-code, or absolute-path access.
- Use only these skills: data_io, data_load, data_preprocess, eda, modeling, evaluation, report.
- Use a descriptive snake_case tool_name for every Step.
- Do not execute the plan. It must be approved by the user first.
""".strip()


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


async def route_request(state: PlanningAgentState, config: RunnableConfig) -> dict[str, Any]:
    """Route the latest turn without exposing Executor mutation Tools."""

    model = _chat_model()
    if model is None:
        return {
            "messages": [
                AIMessage(
                    content=("Planning Agent를 사용하려면 TEST_AGENT_LLM_MODEL을 설정해야 합니다.")
                )
            ],
            "phase": "READY",
        }
    try:
        router = model.with_structured_output(RequestRoute, method="function_calling")
        route = await router.ainvoke(
            [SystemMessage(content=ROUTER_PROMPT), HumanMessage(content=_latest_user_text(state))]
        )
        route = RequestRoute.model_validate(route)
    except Exception as exc:
        return {
            "messages": [AIMessage(content=f"요청 분류에 실패했습니다: {type(exc).__name__}")],
            "phase": "FAILED",
        }
    return {
        "intent": route.intent,
        "phase": "PLANNING" if route.intent == "EXECUTION" else "CHATTING",
        "request_scope_id": _request_scope_id(state, config),
        "plan": None,
        "approved_plan": None,
        "execution_request": None,
        "execution_id": None,
        "event_batch": None,
        "event_history": [],
        "command_receipts": [],
        "next_operation_index": 0,
        "execution_result": None,
        "awaited_event_types": [],
        "awaited_operation_id": None,
        "event_stream_start_id": None,
    }


async def answer_chat(state: PlanningAgentState) -> dict[str, Any]:
    """Answer ordinary conversation with read-only MCP access when it is useful."""

    model = _required_model()
    try:
        tools = await load_executor_read_tools(get_settings())
        agent = create_agent(model, tools, system_prompt=READ_ONLY_PROMPT)
        result = await agent.ainvoke({"messages": state.messages})
        messages = list(result.get("messages", []))
        new_messages = messages[len(state.messages) :]
        if not new_messages:
            raise RuntimeError("Read-only Agent returned no response.")
    except Exception:
        response = await model.ainvoke([SystemMessage(content=READ_ONLY_PROMPT), *state.messages])
        new_messages = [response]
    return {"messages": new_messages, "phase": "READY"}


async def create_plan(state: PlanningAgentState) -> dict[str, Any]:
    """Generate a policy-validated plan without executing it."""

    try:
        planner = _required_model().with_structured_output(ExecutionPlan, method="function_calling")
        plan = await planner.ainvoke(
            [SystemMessage(content=PLANNER_PROMPT), HumanMessage(content=_latest_user_text(state))]
        )
        plan = ExecutionPlan.model_validate(plan)
    except Exception as exc:
        return {
            "messages": [AIMessage(content=f"실행 계획 생성에 실패했습니다: {type(exc).__name__}")],
            "phase": "FAILED",
        }
    return {
        "messages": [
            AIMessage(
                content=(
                    "다음 실행 계획을 검토해 주세요. 승인 전에는 코드를 실행하지 않습니다.\n\n"
                    + render_plan(plan)
                )
            )
        ],
        "plan": plan.model_dump(mode="json"),
        "phase": "AWAITING_APPROVAL",
    }


def review_plan(state: PlanningAgentState) -> dict[str, Any]:
    """Pause for standard Agent Chat UI approve/edit/reject decisions."""

    if state.plan is None:
        raise ValueError("plan is required before HITL review.")
    plan = ExecutionPlan.model_validate(state.plan)
    decision = parse_plan_review(interrupt(plan_review_interrupt(plan)), plan)
    if decision.decision == "REJECT":
        return {
            "messages": [AIMessage(content=decision.message or "실행 계획을 취소했습니다.")],
            "phase": "CANCELLED",
            "approved_plan": None,
        }
    approved = decision.plan
    if approved is None:
        raise ValueError("Approved plan review did not contain a plan.")
    prefix = "수정된 계획을 실행합니다." if decision.decision == "EDIT" else "계획을 승인했습니다."
    return {
        "messages": [AIMessage(content=f"{prefix}\n\n{render_plan(approved)}")],
        "approved_plan": approved.model_dump(mode="json"),
        "phase": "SUBMITTING",
    }


async def submit_plan(state: PlanningAgentState) -> dict[str, Any]:
    """Convert only the human-approved plan into an Executor submission."""

    if state.approved_plan is None or state.request_scope_id is None:
        raise ValueError("approved_plan and request_scope_id are required before submission.")
    plan = ExecutionPlan.model_validate(state.approved_plan)
    operations = plan.operations
    request = AgentExecutionRequest(
        runtime_profile=plan.runtime_profile,
        actor_id=PLANNING_AGENT_ID,
        user_id=get_settings().default_user_id,
        project_id=get_settings().default_project_id,
        session_id=f"planning-session-{state.request_scope_id}",
        task_id=f"planning-task-{state.request_scope_id}",
        operation_mode=plan.operation_mode,
        operation_wait_timeout_seconds=600,
        steps=operations[0].steps,
        follow_up_operations=[operation.steps for operation in operations[1:]],
        auto_finalize=True,
    )
    try:
        receipt = await submit_execution(request, get_settings())
    except Exception as exc:
        return {
            "messages": [AIMessage(content=f"Executor 제출에 실패했습니다: {type(exc).__name__}")],
            "phase": "FAILED",
        }
    return {
        "execution_request": request.model_dump(mode="json"),
        "execution_id": str(receipt["execution_id"]),
        "awaited_event_types": sorted(_wake_event_types(request.operation_mode)),
        "awaited_operation_id": _operation_id(receipt),
        "event_stream_start_id": _stream_start_id(receipt),
        "command_receipts": [*state.command_receipts, receipt],
        "next_operation_index": 0,
        "phase": "WAITING_FOR_EVENT",
    }


async def wait_for_stream_event(state: PlanningAgentState) -> dict[str, Any]:
    """Wait for one relevant Executor boundary while the local Chat UI run is active."""

    if state.execution_id is None or state.event_stream_start_id is None:
        raise ValueError("execution_id and event_stream_start_id are required for event waiting.")
    settings = get_settings()
    try:
        waiter = ExecutionEventWaiter(
            settings.executor_redis_url,
            settings.executor_event_stream,
            f"{settings.executor_consumer_group_prefix}-planning",
            start_id=state.event_stream_start_id,
        )
        await waiter.open()
        try:
            batch = await waiter.wait_for_wakeup(
                state.execution_id,
                timeout_seconds=settings.execution_timeout_seconds,
                event_types=set(state.awaited_event_types) or None,
                operation_id=state.awaited_operation_id,
            )
        finally:
            await waiter.close()
    except Exception as exc:
        return {
            "messages": [
                AIMessage(content=f"Executor 이벤트 대기에 실패했습니다: {type(exc).__name__}")
            ],
            "phase": "FAILED",
        }
    return {"event_batch": batch.model_dump(mode="json"), "phase": "VERIFYING"}


async def verify_execution(state: PlanningAgentState) -> dict[str, Any]:
    """Reconcile the event with MCP state and decide whether MULTI may advance."""

    if state.execution_id is None or state.event_batch is None:
        raise ValueError("execution_id and event_batch are required for verification.")
    batch = ExecutionEventBatch.model_validate(state.event_batch)
    try:
        result = await reconcile_execution(state.execution_id, batch, get_settings())
    except Exception as exc:
        return {
            "messages": [
                AIMessage(content=(f"Executor 결과 확인에 실패했습니다: {exception_summary(exc)}"))
            ],
            "phase": "FAILED",
        }
    status = str(result["status"])
    failed_step = any(event.event_type == "execution.step_failed" for event in batch.events)
    phase = _phase_after_result(status, failed_step, state)
    message = _result_message(result, failed_step)
    return {
        "messages": [AIMessage(content=message)],
        "phase": phase,
        "execution_result": result,
        "execution_request": state.execution_request if phase == "ADVANCING" else None,
        "event_batch": None,
        "event_history": [
            *state.event_history,
            *(event.model_dump(mode="json") for event in batch.events),
        ],
        "awaited_event_types": [],
        "awaited_operation_id": None,
        "event_stream_start_id": None,
    }


async def advance_multi(state: PlanningAgentState) -> dict[str, Any]:
    """Submit the next approved Operation or finalize the MULTI Execution."""

    if (
        state.execution_id is None
        or state.execution_request is None
        or state.execution_result is None
    ):
        raise ValueError("MULTI advancement requires execution state and the approved request.")
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
                actor_id=PLANNING_AGENT_ID,
            )
            return {
                "phase": "WAITING_FOR_EVENT",
                "next_operation_index": operation_index + 1,
                "awaited_event_types": sorted(MULTI_OPERATION_WAKE_EVENT_TYPES),
                "awaited_operation_id": _operation_id(receipt),
                "event_stream_start_id": _stream_start_id(receipt),
                "command_receipts": [*state.command_receipts, receipt],
            }
        receipt = await finalize_execution(
            state.execution_id,
            get_settings(),
            expected_version=expected_version,
            actor_id=PLANNING_AGENT_ID,
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
            "messages": [
                AIMessage(content=f"MULTI 후속 실행에 실패했습니다: {type(exc).__name__}")
            ],
            "phase": "FAILED",
            "execution_request": None,
        }


def _route_after_request(state: PlanningAgentState) -> str:
    if state.phase == "CHATTING":
        return "answer_chat"
    if state.phase == "PLANNING":
        return "create_plan"
    return END


def _route_after_plan(state: PlanningAgentState) -> str:
    return "review_plan" if state.phase == "AWAITING_APPROVAL" else END


def _route_after_review(state: PlanningAgentState) -> str:
    return "submit_plan" if state.phase == "SUBMITTING" else END


def _route_after_submit(state: PlanningAgentState) -> str:
    return "wait_for_stream_event" if state.phase == "WAITING_FOR_EVENT" else END


def _route_after_wait(state: PlanningAgentState) -> str:
    return "verify_execution" if state.phase == "VERIFYING" else END


def _route_after_verify(state: PlanningAgentState) -> str:
    return "advance_multi" if state.phase == "ADVANCING" else END


def _route_after_advance(state: PlanningAgentState) -> str:
    return "wait_for_stream_event" if state.phase == "WAITING_FOR_EVENT" else END


def _required_model() -> ChatOpenAI:
    model = _chat_model()
    if model is None:
        raise RuntimeError("TEST_AGENT_LLM_MODEL is required for the planning Agent.")
    return model


def _latest_user_text(state: PlanningAgentState) -> str:
    return str(_latest_user_message(state).content)


def _latest_user_message(state: PlanningAgentState) -> HumanMessage:
    for message in reversed(state.messages):
        if isinstance(message, HumanMessage):
            return message
    raise ValueError("The planning Agent requires a user message.")


def _request_scope_id(state: PlanningAgentState, config: RunnableConfig) -> str:
    thread_id = str(config.get("configurable", {}).get("thread_id") or "unscoped")
    message = _latest_user_message(state)
    message_identity = message.id or f"message-{len(state.messages)}"
    canonical = f"{thread_id}:{message_identity}:{message.content}"
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    return digest


def _wake_event_types(operation_mode: str) -> set[str]:
    return MULTI_OPERATION_WAKE_EVENT_TYPES if operation_mode == "MULTI" else TERMINAL_EVENT_TYPES


def _operation_id(receipt: dict[str, Any]) -> str | None:
    operation = receipt.get("operation")
    if not isinstance(operation, dict) or operation.get("operation_id") is None:
        return None
    return str(operation["operation_id"])


def _stream_start_id(receipt: dict[str, Any]) -> str:
    value = receipt.get("event_stream_start_id")
    if not isinstance(value, str) or not value:
        raise ValueError("Executor receipt is missing event_stream_start_id.")
    return value


def _phase_after_result(status: str, failed_step: bool, state: PlanningAgentState) -> str:
    if status == "SUCCEEDED":
        return "SUCCEEDED"
    if status == "WAITING_FOR_OPERATION":
        if failed_step:
            return "FAILED"
        if state.execution_request is not None:
            return "ADVANCING"
        return "READY"
    return "FAILED"


def _result_message(result: dict[str, Any], failed_step: bool) -> str:
    output = _step_output_text(result.get("steps", []))
    lines = [
        f"Execution {result['execution_id']} 상태: {result['status']}",
        f"Step 수: {len(result.get('steps', []))}",
    ]
    if failed_step:
        lines.append("현재 Operation에서 오류가 발생하여 승인된 후속 실행을 중단했습니다.")
    if output:
        lines.extend(["실행 결과:", output])
    artifacts = sorted(str(item["name"]) for item in result.get("artifacts", []))
    lines.append(f"Artifacts: {artifacts}")
    lines.append(f"Notebook: {result.get('notebook_path')}")
    return "\n\n".join(lines)


def _step_output_text(steps: list[dict[str, Any]]) -> str:
    rendered: list[str] = []
    for step in steps:
        result = step.get("result", {})
        error = result.get("error_message")
        if error:
            rendered.append(str(error))
        rendered.extend(_render_outputs(result.get("resolved_outputs", [])))
    return "\n".join(part for part in rendered if part)


def _render_outputs(outputs: list[dict[str, Any]]) -> list[str]:
    rendered: list[str] = []
    for output in outputs:
        for representation in output.get("representations", []):
            content = representation.get("content")
            if isinstance(content, str) and content.strip():
                rendered.append(content.strip())
    return rendered


builder = StateGraph(PlanningAgentState)
builder.add_node("route_request", route_request)
builder.add_node("answer_chat", answer_chat)
builder.add_node("create_plan", create_plan)
builder.add_node("review_plan", review_plan)
builder.add_node("submit_plan", submit_plan)
builder.add_node("wait_for_stream_event", wait_for_stream_event)
builder.add_node("verify_execution", verify_execution)
builder.add_node("advance_multi", advance_multi)
builder.add_edge(START, "route_request")
builder.add_conditional_edges(
    "route_request",
    _route_after_request,
    {"answer_chat": "answer_chat", "create_plan": "create_plan", END: END},
)
builder.add_edge("answer_chat", END)
builder.add_conditional_edges(
    "create_plan", _route_after_plan, {"review_plan": "review_plan", END: END}
)
builder.add_conditional_edges(
    "review_plan", _route_after_review, {"submit_plan": "submit_plan", END: END}
)
builder.add_conditional_edges(
    "submit_plan",
    _route_after_submit,
    {"wait_for_stream_event": "wait_for_stream_event", END: END},
)
builder.add_conditional_edges(
    "wait_for_stream_event",
    _route_after_wait,
    {"verify_execution": "verify_execution", END: END},
)
builder.add_conditional_edges(
    "verify_execution", _route_after_verify, {"advance_multi": "advance_multi", END: END}
)
builder.add_conditional_edges(
    "advance_multi",
    _route_after_advance,
    {"wait_for_stream_event": "wait_for_stream_event", END: END},
)

graph = builder.compile()
