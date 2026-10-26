"""Pure routing functions for the orchestration graph."""

from __future__ import annotations

from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.graphs.nodes.generate_report import execution_is_reportable
from app.schemas.common.workflow_hitl_actions import get_action


def route_user_request_result(state: AnalysisWorkflowState) -> str:
    return state["routing_result"]["route"]

def route_next_user_request(state: AnalysisWorkflowState) -> str:
    return (
        "route_user_request"
        if state["conversation_control"]["action"] == "continue"
        else "cancel_request"
    )


def route_workflow_recommendation(state: AnalysisWorkflowState) -> str:
    return ("select_workflow_candidate" if state.get("recommendation", {}).get("recommendation_available")
            and state.get("workflow_candidates") else "generate_workflow")


def route_workflow_status(state: AnalysisWorkflowState) -> str:
    if state.get("tool_input_payload_version") == 2 and not state.get("tool_inputs_confirmed"):
        return "collect_missing_information"
    status = state["workflow_status"]
    if status == "needs_input":
        return "collect_missing_information"
    if status == "ready":
        return "review_workflow"
    return "workflow_unavailable"


def route_workflow_candidate_selection(state: AnalysisWorkflowState) -> str:
    intent = state["workflow_candidate_selection"].get("intent")
    if intent:
        return get_action("candidate", intent).destination or route_workflow_status(state)
    if not state["workflow_candidate_selection"]["approved"]:
        return "route_user_request"
    return route_workflow_status(state)


def route_approval(state: AnalysisWorkflowState) -> str:
    return (
        "save_approved_workflow"
        if state["approval"]["approved"]
        else "route_user_request"
    )


def route_after_execution_start(state: AnalysisWorkflowState) -> str:
    return "register_execution" if state.get("execution_id") else "end"


def route_after_adaptive_submit(state: AnalysisWorkflowState) -> str:
    return "wait_executor_event" if state.get("execution_id") else "end"


def route_after_finalize(state: AnalysisWorkflowState) -> str:
    if state.get("adaptive_status") in {"cancellation_failed", "terminal"}:
        return "end"
    return "wait_executor_event" if state.get("execution_id") else "end"


def route_redis_execution_event(state: AnalysisWorkflowState) -> str:
    if state.get("execution_mode") == "SINGLE":
        return "collect_static_execution_results"
    if state.get("executor_wait_phase") == "execution_completed":
        return "collect_final_execution_event"
    return "collect_adaptive_execution_results"


def route_after_static_results(state: AnalysisWorkflowState) -> str:
    return "record_static_executor_receipt"


def route_report_generation(state: AnalysisWorkflowState) -> str:
    return (
        "generate_report"
        if execution_is_reportable(state)
        else "skip_execution_report"
    )


def route_after_adaptive_collection(state: AnalysisWorkflowState) -> str:
    return "record_adaptive_executor_receipt"


def route_after_adaptive_results(state: AnalysisWorkflowState) -> str:
    # Terminal Executor failures require no cancellation or further cells.
    if state.get("execution_status") in {"FAILED", "CANCELLED"}:
        return "skip_execution_report"
    if state.get("result_collection_error"):
        return "cancel_adaptive_execution"
    has_failed_cells = any(
        (item.get("result") or {}).get("status") == "FAILED"
        for item in state.get("executor_tool_results", [])
        if isinstance(item, dict)
    )
    has_unexecuted_cells = any(
        (item.get("result") or {}).get("status")
        in {"NOT_EXECUTED", "PENDING", "SKIPPED"}
        for item in state.get("executor_tool_results", [])
        if isinstance(item, dict)
    )
    # Executor는 한 셀이 실패하면 같은 Operation의 후속 셀을 건너뛴다.
    # 실패한 셀의 출력 변수가 생성되지 않은 상태에서 후속 셀만 새
    # Operation으로 재제출하면 NameError가 연쇄 발생하므로 즉시 종료한다.
    if has_failed_cells:
        return "cancel_adaptive_execution"
    if has_unexecuted_cells:
        return "build_next_adaptive_code"
    return (
        "decide_conditional_tools"
        if state.get("adaptive_pending_tool_id")
        else "finalize_adaptive_execution"
    )


def route_after_adaptive_code(state: AnalysisWorkflowState) -> str:
    cells = state.get("notebook", {}).get("cells") or []
    return "submit_adaptive_operation" if cells else "finalize_adaptive_execution"
