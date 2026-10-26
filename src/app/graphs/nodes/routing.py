"""Request receipt and top-level Routing Agent node."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from langchain_core.runnables import RunnableConfig

from agent_config import (
    LOCAL_MOCK_PROJECT_ID,
    LOCAL_MOCK_SESSION_ID,
    LOCAL_MOCK_USER_ID,
    build_langgraph_thread_id,
)
from app.context.builder import context_payload, message_dict, user_message
from app.context.records import snapshot_analysis
from app.graphs.nodes.flow_control import reset_analysis_state
from app.agents.orchestration.agent_interfaces import ainvoke_typed
from app.agents.orchestration.dependencies import AgentDependencies
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.schemas.agents.orchestration_schema import RequestContext, RoutingOutput

ALLOWED_ROUTES = {
    "main": {
        "analysis",
        "analysis_question",
        "faq",
        "cancel",
    },
    "workflow_rejected": {
        "analysis",
        "analysis_question",
        "faq",
        "revise_workflow",
        "cancel",
    },
}

def _message_content(message: Any) -> str:
    """Extract plain user text from Agent Chat message representations."""
    content = (
        message.get("content")
        if isinstance(message, dict)
        else getattr(message, "content", "")
    )
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts).strip()
    return ""


def receive_request(
    state: AnalysisWorkflowState,
    config: RunnableConfig,
) -> dict:
    request = (state.get("user_request") or "").strip()
    messages = state.get("messages") or []
    last = message_dict(messages[-1]) if messages else {}
    # A messages-only next turn must win over the checkpoint's stale user_request.
    fresh_message = (user_message(last) and (not last.get("id") or last.get("id") != state.get("last_received_message_id")))
    request_from_messages = bool(fresh_message and _message_content(last))
    if request_from_messages:
        request = _message_content(last)
    message_id = last.get("id") if request_from_messages else str(uuid4())
    if not request:
        raise ValueError("user_request is required")

    configurable = (config or {}).get("configurable") or {}
    runtime_thread_id = configurable.get("thread_id")
    session_id = (
        state.get("session_id")
        or runtime_thread_id
        or LOCAL_MOCK_SESSION_ID
    )
    context = RequestContext.model_validate(
        {
            "user_id": state.get("user_id") or LOCAL_MOCK_USER_ID,
            "project_id": state.get("project_id") or LOCAL_MOCK_PROJECT_ID,
            "session_id": session_id,
        }
    )
    context_payload = context.model_dump(mode="json")
    thread_id = state.get("thread_id") or build_langgraph_thread_id(
        context.session_id
    )

    records = dict(state.get("analysis_records") or {})
    if state.get("task_id") and state["task_id"] not in records:
        records.update(snapshot_analysis(state).get("analysis_records", {}))
    # TODO(CRUD): 사용자 메시지 POST
    return {
        "conversation_summary": state.get("conversation_summary", ""),
        "summarized_through_message_id": state.get("summarized_through_message_id"),
        "analysis_records": records,
        "current_analysis_task_id": state.get("current_analysis_task_id") or state.get("task_id"),
        **context_payload,
        "user_request": request,
        "request_id": state.get("request_id") or str(uuid4()),
        "thread_id": thread_id,
        "routing_context": "main",
        "conversation_control": {},
        "service_response": {},
        "final_response": {},
        "approval_feedback": None,
        "referenced_analysis_task_ids": [],
        "analysis_reference_ambiguous": False,
        "last_received_message_id": message_id,
        "excluded_workflow_candidates": [],
        "additional_information": state.get("additional_information") or {},
        "workflow_revision": state.get("workflow_revision", 0),
        "return_to": "main_conversation",
        "action_query": None,
        # Agent Chat already placed the user message in state. Avoid adding it
        # twice; direct API callers still receive the normalized message.
        "messages": (
            []
            if request_from_messages
            else [{"role": "user", "content": request, "id": message_id}]
        ),
    }


def ensure_analysis_task(state: AnalysisWorkflowState) -> dict:
    """Create one task id per analysis while keeping the conversation thread."""
    task_id = str(uuid4())
    records = dict(state.get("analysis_records") or {})
    # Old checkpoints may predate analysis_records; archive before resetting.
    if state.get("task_id") and state["task_id"] not in records:
        records.update(snapshot_analysis(state).get("analysis_records", {}))
    records[task_id] = {"task_id": task_id, "original_request": state.get("user_request", "")}
    reset = reset_analysis_state(state) if state.get("task_id") else {}
    if state.get("routing_context") == "workflow_rejected" and reset:
        reset["workflow_revision"] = state.get("workflow_revision", 0)
    return {**reset, "previous_workflow_definition": {},
            "task_id": task_id, "current_analysis_task_id": task_id,
            "analysis_records": records, "referenced_analysis_task_ids": [],
            "analysis_reference_ambiguous": False}


def make_route_user_request(deps: AgentDependencies):
    """Create a node that classifies the user's top-level route."""

    async def route_user_request(state: AnalysisWorkflowState) -> dict:
        output = await ainvoke_typed(
            deps.routing_agent,
            context_payload(deps, state, "routing", {
                "user_request": state["user_request"],
                "routing_context": state["routing_context"],
                "approval_feedback": state.get("approval_feedback"),
            }),
            RoutingOutput,
        )

        if output.route not in ALLOWED_ROUTES[state["routing_context"]]:
            raise ValueError(
                f"{state['routing_context']}에서 허용되지 않는 route입니다: "
                f"{output.route}"
            )
        # TODO(CRUD): 에이전트 메시지 POST
        payload = output.model_dump(mode="json")
        update = {"routing_result": payload}
        if output.route == "analysis_question":
            records = state.get("analysis_records") or {}
            ids = list(dict.fromkeys(output.analysis_task_ids))
            ambiguous = output.reference_ambiguous
            if not ids and not ambiguous:
                current = state.get("current_analysis_task_id")
                ids = [current] if current in records else []
            if any(task_id not in records for task_id in ids):
                ambiguous = True
                ids = []
            update.update(referenced_analysis_task_ids=ids,
                          analysis_reference_ambiguous=ambiguous or not ids)
            if len(ids) == 1 and not ambiguous:
                update["current_analysis_task_id"] = ids[0]
        return update

    return route_user_request


__all__ = ["ensure_analysis_task", "make_route_user_request", "receive_request"]
