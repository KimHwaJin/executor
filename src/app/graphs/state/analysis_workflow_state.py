"""JSON-serializable LangGraph state for user-agent orchestration."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from app.agent_worker.graph_boundary import ExecutorBoundaryState
from app.context.records import AnalysisRecord
from app.graphs.message_utils import append_messages_with_ids


class AnalysisWorkflowState(ExecutorBoundaryState, total=False):
    conversation_summary: str
    summarized_through_message_id: str | None
    current_analysis_task_id: str | None
    analysis_records: dict[str, AnalysisRecord]
    referenced_analysis_task_ids: list[str]
    analysis_reference_ambiguous: bool
    last_received_message_id: str | None
    request_id: str
    user_id: str
    project_id: str
    session_id: str
    thread_id: str
    user_request: str
    action_query: str | None
    return_to: str
    conversation_control: dict[str, Any]
    service_response: dict[str, Any]
    messages: Annotated[list[dict[str, Any]], append_messages_with_ids]
    routing_context: Literal["main", "workflow_rejected"]
    routing_result: dict[str, Any]
    analysis_intent: dict[str, Any]
    data_selection: dict[str, Any]
    analysis_context: dict[str, Any]
    artifact_output_dir: str
    artifact_files: dict[str, Any]
    recommendation: dict[str, Any]
    recommendation_search: dict[str, Any]
    skip_workflow_recommendation: bool
    append_workflow_recommendations: bool
    workflow_candidates: list[dict[str, Any]]
    generated_workflow_drafts: list[dict[str, Any]]
    workflow_generation_notice: str | None
    workflow_candidate_selection: dict[str, Any]
    candidate_selection_payload_version: int
    excluded_workflow_candidates: list[dict[str, Any]]
    workflow_origin: str
    additional_information: dict[str, Any]
    workflow_definition: dict[str, Any]
    workflow_input_definition: dict[str, Any]
    tool_input_payload_version: int
    plan_review_payload_version: int
    tool_inputs_confirmed: bool
    previous_workflow_definition: dict[str, Any]
    workflow_id: str
    workflow_version_id: str | None
    workflow_content_revision: int
    execution_inputs: list[dict[str, Any]]
    input_errors: str
    workflow: dict[str, Any]
    workflow_status: str
    workflow_revision: int
    approval: dict[str, Any]
    approval_feedback: str | None
    notebook: dict[str, Any]
    adaptive_pending_tool_id: str | None
    adaptive_generated_tool_ids: list[str]
    adaptive_executed_tool_ids: list[str]
    adaptive_observations: dict[str, Any]
    adaptive_runtime_decisions: dict[str, str]
    adaptive_execution_plan: dict[str, Any]
    adaptive_decision_history: list[dict[str, Any]]
    adaptive_round: int
    adaptive_status: str
    execution_mode: str
    idempotency_key: str
    executor_operation_id: str
    executor_operation_steps: list[dict[str, Any]]
    executor_state_version: int
    executor_next_sequence: int
    executor_operation_number: int
    executor_requires_state_version: bool
    executor_wait_phase: Literal["operation_completed", "execution_completed"]
    execution_event: dict[str, Any] | None
    executor_tool_results: list[dict[str, Any]]
    executor_result_history: list[dict[str, Any]]
    executor_request_path: str
    execution_steps: list[dict[str, Any]]
    execution_events: list[dict[str, Any]]
    step_results: list[dict[str, Any]]
    executor_submit_response: dict[str, Any]
    execution_status: str
    result_collection_error: str
    analysis_report: dict[str, Any]
    report_status: str
    report_artifact_request: dict[str, Any]
    report_artifact_response: dict[str, Any]
    final_response: dict[str, Any]


__all__ = ["AnalysisWorkflowState"]
