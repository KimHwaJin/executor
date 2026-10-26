"""Read-only approval of the exact compiled workflow and confirmed inputs."""
from copy import deepcopy

from jsonschema import Draft202012Validator
from langgraph.types import interrupt

from app.graphs.hitl_protocol import build_hitl_request, unwrap_hitl_response
from app.graphs.message_utils import as_message_content
from app.schemas.common.workflow_hitl_actions import get_action, transport_actions
from app.schemas.common.workflow_hitl_schema import PlanReviewDisplay, build_plan_review_submission_schema


def review_plan_v2(state: dict) -> dict:
    if not state.get("tool_inputs_confirmed") or state.get("input_errors"):
        raise ValueError("입력값 확인을 완료한 뒤 계획을 승인할 수 있습니다.")
    actions = [get_action("plan", intent) for intent in ("approve", "reject")]
    display = PlanReviewDisplay(
        workflow_id=state["workflow_id"], workflow_revision=state["workflow_revision"],
        workflow=deepcopy(state["workflow"]),
        execution_inputs=[{k: deepcopy(v[k]) for k in ("step_index", "tool_index", "parameter", "value")}
                          for v in state.get("execution_inputs", [])],
        available_actions=[{"intent": a.intent, "label": a.label} for a in actions],
    )
    schema = build_plan_review_submission_schema(display)
    request = build_hitl_request(
        action_name="workflow_approval", description="완성된 워크플로우와 입력값을 확인하고 실행을 승인해주세요.",
        args=display.model_dump(mode="json"), args_schema=schema, payload_version=2,
        allowed_decisions=transport_actions("plan", [a.intent for a in display.available_actions]),
    )
    raw = interrupt(request)
    if isinstance(raw, dict) and set(raw) == {"resume"}:
        raw = raw["resume"]
    approved = False
    # Inspect the decision before unwrap: approve normally becomes default_args.
    # A submitted display object or an edit must never count as approval.
    if isinstance(raw, dict) and "decisions" in raw:
        decisions = raw["decisions"]
        if set(raw) != {"decisions"} or not isinstance(decisions, list) or len(decisions) != 1:
            raise ValueError("계획 승인에는 하나의 decision이 필요합니다.")
        decision = decisions[0]
        if not isinstance(decision, dict):
            raise ValueError("잘못된 계획 승인 decision입니다.")
        approved = decision == {"type": "approve"}
        if not approved:
            edited = decision.get("edited_action")
            if (set(decision) != {"type", "edited_action"} or decision.get("type") != "edit"
                    or not isinstance(edited, dict) or set(edited) != {"name", "args"}
                    or edited["name"] != "workflow_approval"):
                raise ValueError("승인 또는 이유가 포함된 edit 거절을 제출해주세요.")
    reason = None
    if not approved:
        answer = unwrap_hitl_response(raw, default_args=request["action_requests"][0]["args"])
        Draft202012Validator(schema).validate(answer)
        reason = get_action("plan", "reject").submission_model.model_validate(answer).reason
    decision = {"approved": approved, "feedback": reason}
    updates = {"approval": decision, "approval_feedback": reason,
               "messages": [{"role": "user", "name": "workflow_approval", "content": as_message_content(decision)}]}
    if not approved:
        updates.update(user_request=reason.strip(), routing_context="workflow_rejected")
    return updates
