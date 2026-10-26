"""Conversation hubs, state reset, and cancellation nodes."""

from __future__ import annotations

from app.graphs.message_utils import as_message_content
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState


def announce_workflow_search(state: AnalysisWorkflowState) -> dict:
    # TODO(CRUD): 에이전트 메시지 POST
    return {
        "previous_workflow_definition": (
            state.get("workflow_definition") or state.get("previous_workflow_definition", {})
        ) if state.get("routing_context") == "workflow_rejected" else {},
        "workflow_candidates": [],
        "generated_workflow_drafts": [],
        "workflow_generation_notice": None,
        "append_workflow_recommendations": False,
        "recommendation": {},
        "recommendation_search": {},
        "candidate_selection_payload_version": 2,
        "workflow_candidate_selection": {},
        "workflow": {},
        "workflow_definition": {},
        "workflow_input_definition": {},
        "tool_input_payload_version": 1,
        "tool_inputs_confirmed": False,
        "plan_review_payload_version": 1,
        "workflow_id": "",
        "execution_inputs": [],
        "input_errors": "",
        "workflow_version_id": None,
        "workflow_content_revision": 1,
        "workflow_status": "",
        "workflow_origin": "",
        "approval": {},
        "messages": [
            {
                "role": "agent",
                "name": "workflow_generator",
                "content": as_message_content(
                    {
                        "status": "preparing_candidates",
                        "message": (
                            "사용자 요청에 맞는 "
                            "워크플로우 후보를 준비하고 있습니다."
                        ),
                    }
                ),
            }
        ]
    }


def reset_analysis_state(state: AnalysisWorkflowState) -> dict:
    """Clear analysis state before generating another Workflow."""
    return {
        "analysis_intent": {},
        "execution_event": None,
        "result_collection_error": "",
        "ew_pending": {},
        "ew_receipts": {},
        "ew_sequences": {},
        "data_selection": {},
        "analysis_context": {},
        "task_id": "",
        "artifact_output_dir": "",
        "artifact_files": {},
        "recommendation": {},
        "recommendation_search": {},
        "skip_workflow_recommendation": False,
        "excluded_workflow_candidates": [],
        "workflow_generation_notice": None,
        "generated_workflow_drafts": [],
        "previous_workflow_definition": state.get("workflow_definition", {}),
        "workflow_candidates": [],
        "append_workflow_recommendations": False,
        "workflow_candidate_selection": {},
        "workflow_origin": "",
        "additional_information": {},
        "workflow": {},
        "workflow_definition": {},
        "workflow_input_definition": {},
        "tool_input_payload_version": 1,
        "tool_inputs_confirmed": False,
        "plan_review_payload_version": 1,
        "workflow_id": "",
        "execution_inputs": [],
        "input_errors": "",
        "workflow_version_id": None,
        "workflow_content_revision": 1,
        "workflow_status": "",
        "workflow_revision": 0,
        "approval": {},
        "approval_feedback": None,
        "notebook": {},
        "adaptive_pending_tool_id": None,
        "adaptive_generated_tool_ids": [],
        "adaptive_executed_tool_ids": [],
        "adaptive_observations": {},
        "adaptive_runtime_decisions": {},
        "adaptive_execution_plan": {},
        "adaptive_decision_history": [],
        "adaptive_round": 0,
        "adaptive_status": "",
        "execution_mode": "",
        "idempotency_key": "",
        "execution_id": "",
        "executor_operation_id": "",
        "executor_operation_steps": [],
        "executor_state_version": 0,
        "executor_next_sequence": 0,
        "executor_operation_number": 0,
        "executor_requires_state_version": False,
        "executor_wait_phase": "execution_completed",
        "executor_tool_results": [],
        "executor_result_history": [],
        "executor_request_path": "",
        "execution_steps": [],
        "execution_events": [],
        "step_results": [],
        "executor_submit_response": {},
        "execution_status": "",
        "analysis_report": {},
        "report_status": "",
        "report_artifact_request": {},
        "report_artifact_response": {},
        "final_response": {},
        "action_query": None,
        "return_to": "main_conversation",
    }


def cancel_request(state: AnalysisWorkflowState) -> dict:
    """End the current run without deleting its checkpoint thread."""
    return {
        "final_response": {
            "status": "cancelled",
            "thread_id": state["thread_id"],
            "message": "사용자 요청으로 현재 작업을 종료했습니다.",
        },
        "messages": [
            {
                "role": "assistant",
                "name": "cancel_request",
                "content": "사용자 요청으로 현재 작업을 종료했습니다.",
            }
        ],
    }
