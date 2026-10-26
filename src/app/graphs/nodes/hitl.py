"""Dynamic HITL interrupt nodes for selection, questions, and review."""

from __future__ import annotations

from copy import deepcopy
import json
import logging

from agent_config import AgentSettings
from langchain_core.runnables import RunnableConfig
from app.graphs.hitl_protocol import request_human_input
from jsonschema import Draft202012Validator
from app.graphs.nodes.candidate_contract import candidate_display
from app.schemas.common.workflow_hitl_actions import dispatch_candidate, transport_actions
from app.schemas.common.workflow_hitl_schema import (
    CANDIDATE_SUBMISSION, build_candidate_submission_schema,
)
from app.graphs.message_utils import as_message_content
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.services.demo_artifact_store import (
    build_workflow_candidate_artifact_path,
    write_demo_json,
)
from app.schemas.agents.orchestration_schema import (
    AdditionalInformationResponse,
    ApprovalDecision,
    NextUserRequestResponse,
    WorkflowCandidateSelectionResponse,
)


logger = logging.getLogger(__name__)


def _coerce_answer(value, definition: dict):
    input_type = str(definition.get("type", "")).lower()
    if not (
        input_type == "list"
        or input_type.startswith("list[")
        or input_type == "array"
    ):
        return value
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            import json

            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return parsed
        return [item.strip() for item in stripped.split(",") if item.strip()]
    return value


def _coerce_answers_by_input_schema(
    answers: dict,
    input_schema: dict,
) -> dict:
    return {
        name: _coerce_answer(value, input_schema.get(name, {}))
        for name, value in answers.items()
    }


def collect_missing_information(state: AnalysisWorkflowState, config: RunnableConfig = None) -> dict:
    if state.get("workflow_definition"):
        from app.graphs.nodes.workflow_inputs import collect_standard_inputs
        return collect_standard_inputs(state, config)
    workflow = state["workflow"]["workflow"]
    input_schema = workflow.get("input_schema", {})
    unresolved = [
        {
            **item,
            "input_schema": input_schema.get(item.get("name"), {}),
        }
        for item in workflow.get("unresolved_inputs", [])
    ]
    description = "Workflow 완성에 필요한 정보를 입력해주세요."
    form_args = {}
    form_properties = {}
    required_fields = []
    for item in unresolved:
        name = item.get("name")
        if not name:
            continue
        definition = item.get("input_schema") or {}
        input_type = str(definition.get("type") or "str").lower()
        if input_type in {"int", "integer"}:
            json_type, default = "integer", 0
        elif input_type in {"float", "number"}:
            json_type, default = "number", 0
        elif input_type in {"bool", "boolean"}:
            json_type, default = "boolean", False
        elif input_type == "list" or input_type.startswith("list["):
            json_type, default = "array", []
        else:
            json_type, default = "string", ""

        form_args[name] = definition.get("default", default)
        field_schema = {
            "type": json_type,
            "title": name,
            "description": item.get("question") or name,
        }
        if json_type == "string" and definition.get("required", True):
            field_schema["minLength"] = 1
        if json_type == "array":
            field_schema["items"] = {}
        form_properties[name] = field_schema
        if definition.get("required", True):
            required_fields.append(name)

    form_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": form_properties,
        "required": required_fields,
    }
    response = request_human_input(
        action_name="additional_information",
        description=description,
        args=form_args,
        allowed_decisions=["approve", "edit"],
        args_schema=form_schema,
    )
    if isinstance(response, dict) and "answers" not in response:
        response = {"answers": response}
    answers = AdditionalInformationResponse.model_validate(response)
    # TODO(CRUD): 사용자 메시지 POST
    coerced_answers = _coerce_answers_by_input_schema(
        answers.answers,
        input_schema,
    )
    merged = {**state.get("additional_information", {}), **coerced_answers}
    return {
        "additional_information": merged,
    }


def _workflow_candidate_description(
    candidates: list[dict],
    intro: str,
) -> str:
    """Build a compact, CLI-level Markdown summary for candidate review."""
    sections = [intro]
    for index, candidate in enumerate(candidates, start=1):
        workflow = candidate.get("workflow", {}).get("workflow", {})
        name = workflow.get("name") or candidate.get("id")
        status = workflow.get("status") or candidate.get("status") or "unknown"
        origin = candidate.get("origin") or "unknown"
        execution_mode = workflow.get("execution_mode") or "static"
        lines = [
            f"### {index}. {name}",
            f"- ID: `{candidate.get('id')}`",
            f"- 상태: `{status}` / 출처: `{origin}` / 실행 모드: `{execution_mode}`",
        ]
        goal = workflow.get("user_request") or workflow.get("goal")
        if goal:
            lines.append(f"- 요청: {goal}")
        unresolved = workflow.get("unresolved_inputs") or []
        unresolved_names = [
            item.get("name")
            for item in unresolved
            if isinstance(item, dict) and item.get("name")
        ]
        if unresolved_names:
            lines.append(f"- 필요 입력: {', '.join(unresolved_names)}")

        steps = workflow.get("steps") or []
        if steps:
            lines.append("- Steps:")
        for step_index, step in enumerate(steps):
            lines.append(f"  - Step {step_index}: `{step.get('skill')}`")
            for tool_index, tool in enumerate(step.get("tools") or []):
                tool_execution = tool.get("execution") or "always"
                condition = tool.get("condition")
                condition_text = ""
                if tool_execution == "conditional" and isinstance(condition, dict):
                    decision_id = condition.get("decision_id")
                    condition_text = (
                        f", decision_id: {decision_id}" if decision_id else ""
                    )
                lines.append(
                    f"    - Tool {tool_index}: `{tool.get('tool')}` [{tool_execution}{condition_text}]"
                )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def make_select_workflow_candidate(settings: AgentSettings):
    def select_workflow_candidate(state: AnalysisWorkflowState) -> dict:
        candidates = state.get("workflow_candidates", [])
        if state.get("candidate_selection_payload_version", 1) == 2:
            display = candidate_display(
                candidates,
                recommendation_search=state.get("recommendation_search") if candidates and candidates[0]["origin"] == "recommended" else None,
                can_recommend_again=settings.workflow_recommendation_enabled,
                can_regenerate=state.get("workflow_revision", 0) < settings.max_workflow_revisions,
                can_generate=state.get("workflow_revision", 0) < settings.max_workflow_revisions,
            )
            schema = build_candidate_submission_schema(display)
            response = request_human_input(
                action_name="workflow_candidate_selection",
                description="워크플로우 후보를 확인하고 선택해 주세요.",
                args=display.model_dump(mode="json"),
                allowed_decisions=transport_actions("candidate", [a.intent for a in display.available_actions]),
                args_schema=schema, payload_version=2,
            )
            # Agent Chat edits display args field by field. Allow its Available
            # Actions field to carry the submission without changing the API
            # contract or trusting the echoed candidate definitions.
            if isinstance(response, dict) and set(response) == {"candidates", "available_actions"}:
                submission = response["available_actions"]
                if isinstance(submission, str):
                    try:
                        submission = json.loads(submission)
                    except json.JSONDecodeError:
                        pass
                if isinstance(submission, dict):
                    response = submission
            # Direct graph/CLI callers must obey the same constraints as the API.
            Draft202012Validator(schema).validate(response)
            answer = CANDIDATE_SUBMISSION.validate_python(response)
            payload = answer.model_dump(mode="json", exclude_unset=True)
            outcome = dispatch_candidate(state, answer)
            if outcome.selected_candidate_id is None:
                return {**outcome.updates, "workflow_candidate_selection": payload}
            selected_candidate_id = outcome.selected_candidate_id
        else:
            description = "Workflow 후보를 선택하거나 피드백과 함께 거절해주세요."
            description = _workflow_candidate_description(candidates, description)

            selection_args = {
                "candidate_number": 1,
                "workflow_overview": description,
            }
            selection_schema = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "candidate_number": {
                        "type": "integer",
                        "title": "Candidate Number",
                        "minimum": 1,
                        "maximum": max(1, len(candidates)),
                    },
                    "workflow_overview": {
                        "type": "string",
                        "title": "Workflow Overview",
                        "description": "검토용 요약입니다. 후보 선택은 Candidate Number로 변경하세요.",
                        "readOnly": True,
                    },
                },
                "required": ["candidate_number", "workflow_overview"],
            }
            response = request_human_input(
                action_name="workflow_candidate_selection",
                description=description,
                args=selection_args,
                allowed_decisions=["approve", "edit", "reject"],
                args_schema=selection_schema,
            )
            if isinstance(response, str):
                stripped = response.strip()
                if stripped.isdigit():
                    candidate_index = int(stripped) - 1
                    if 0 <= candidate_index < len(candidates):
                        stripped = str(candidates[candidate_index].get("id") or "")
                response = {"selected_candidate_id": stripped}
            if isinstance(response, bool):
                response = {"approved": response}
            if isinstance(response, dict) and "candidate_number" in response:
                candidate_index = int(response["candidate_number"]) - 1
                if not 0 <= candidate_index < len(candidates):
                    raise ValueError(
                        f"candidate_number must be between 1 and {len(candidates)}"
                    )
                response = {
                    "approved": True,
                    "selected_candidate_id": candidates[candidate_index].get("id"),
                    "feedback": None,
                }
            selection = WorkflowCandidateSelectionResponse.model_validate(response)
            payload = selection.model_dump(mode="json")
            if not selection.approved:
                return {
                    "workflow_candidate_selection": payload,
                    "approval_feedback": selection.feedback,
                    "user_request": selection.feedback.strip(),
                    "routing_context": "workflow_rejected",
                }

            selected_candidate_id = selection.selected_candidate_id

        selected = next(
            (
                candidate
                for candidate in candidates
                if candidate.get("id") == selected_candidate_id
            ),
            None,
        )
        if selected is None:
            raise ValueError(
                f"unknown workflow candidate: {selected_candidate_id!r}"
            )
        definition = deepcopy(selected["workflow"])
        execution_inputs = deepcopy(selected.get("execution_inputs", []))
        if definition.get("workflow_version") == "1.0":
            from app.agents.workflow_generator.workflow_standard_compiler import compile_workflow
            workflow = compile_workflow(definition, workflow_id=selected["workflow_id"],
                                        execution_inputs=execution_inputs)
        else:
            workflow = deepcopy(definition)
        workflow["workflow"].setdefault("context", {})["output_dir"] = state.get("artifact_output_dir", "")
        artifact_files = dict(state.get("artifact_files", {}))
        workflow_status = workflow["workflow"]["status"]

        return {
            "workflow_candidate_selection": payload,
            "workflow": workflow,
            "workflow_definition": definition if definition.get("workflow_version") == "1.0" else {},
            "workflow_input_definition": {},
            "tool_input_payload_version": 2 if (definition.get("workflow_version") == "1.0"
                and state.get("candidate_selection_payload_version") == 2) else 1,
            "tool_inputs_confirmed": False,
            "plan_review_payload_version": 1,
            "workflow_id": selected.get("workflow_id", ""),
            "execution_inputs": execution_inputs,
            "input_errors": "",
            "workflow_version_id": selected.get("version_id"),
            "workflow_content_revision": selected.get("revision", 1),
            "workflow_status": workflow_status,
            "workflow_origin": selected["origin"],
            "approval": {},
            "approval_feedback": None,
            "artifact_files": artifact_files,
        }

    return select_workflow_candidate

def review_workflow(state: AnalysisWorkflowState) -> dict:
    logger.debug("[DEBUG] ENTER review_workflow task_id=%s", state.get("task_id"))
    if state.get("workflow_status") != "ready":
        raise ValueError("필수 Tool 입력을 모두 채운 뒤 실행을 승인할 수 있습니다.")
    if state.get("plan_review_payload_version") == 2:
        from app.graphs.nodes.plan_review_v2 import review_plan_v2
        return review_plan_v2(state)
    intro = "Workflow와 입력값을 확인하고 실행을 최종 승인해주세요."
    description = _workflow_candidate_description(
        [
            {
                "id": state["workflow"]["workflow"].get("id", "workflow"),
                "origin": state["workflow_origin"],
                "status": state["workflow"]["workflow"].get("status"),
                "workflow": state["workflow"],
            }
        ],
        intro,
    )
    if state.get("execution_inputs"):
        import json
        description += "\n\n### 이번 실행 입력값\n" + json.dumps(
            state["execution_inputs"], ensure_ascii=False, indent=2,
        )
    review_args = {
        "kind": "workflow_approval",
        "workflow_origin": state["workflow_origin"],
        "workflow_overview": description,
        "workflow": deepcopy(state["workflow"]),
    }
    review_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {
                "type": "string",
                "title": "Kind",
                "readOnly": True,
            },
            "workflow_origin": {
                "type": "string",
                "title": "Workflow Origin",
                "readOnly": True,
            },
            "workflow": {
                "type": "object",
                "title": "실행 계획",
                "description": "도구, 단계, 의존성, 파라미터를 표시하기 위한 읽기 전용 실행 정의",
                "readOnly": True,
            },
            "workflow_overview": {
                "type": "string",
                "title": "Workflow Overview",
                "description": "승인 검토용 요약입니다. 전체 실행 정의는 graph state에 보관됩니다.",
                "readOnly": True,
            },
        },
        "required": ["kind", "workflow_origin", "workflow_overview"],
    }
    response = request_human_input(
        action_name="workflow_approval",
        description=intro,
        args=review_args,
        allowed_decisions=["approve", "reject"],
        args_schema=review_schema,
    )
    logger.debug(
        "[DEBUG] review_workflow approval response received task_id=%s response_type=%s",
        state.get("task_id"), type(response).__name__,
    )
    if isinstance(response, bool):
        response = {"approved": response}
    if isinstance(response, dict) and "workflow_overview" in response:
        response = {"approved": True, "feedback": None}
    decision = ApprovalDecision.model_validate(response)
    payload = decision.model_dump(mode="json")
    logger.debug(
        "[DEBUG] review_workflow approval decision complete task_id=%s approved=%s",
        state.get("task_id"), decision.approved,
    )
    # TODO(CRUD): 사용자 메시지 POST
    # TODO(CRUD): 승인/거절 결과 PATCH
    updates = {
        "approval": payload,
        "approval_feedback": None if decision.approved else decision.feedback,
        "messages": [
            {
                "role": "user",
                "name": "workflow_approval",
                "content": as_message_content(payload),
            }
        ],
    }
    if not decision.approved:
        updates.update(
            {
                "user_request": decision.feedback.strip(),
                "routing_context": "workflow_rejected",
            }
        )
    return updates


def await_next_user_request(state: AnalysisWorkflowState) -> dict:
    description = "추가 요청을 입력하거나 대화를 종료해주세요."
    response = request_human_input(
        action_name="next_user_request",
        description=description,
        args={
            "kind": "next_user_request",
            "message": description,
            "options": ["continue", "cancel"],
            "schema": NextUserRequestResponse.model_json_schema(),
        },
    )
    decision = NextUserRequestResponse.model_validate(response)
    payload = decision.model_dump(mode="json")
    update: dict = {
        "conversation_control": payload,
        "return_to": "main_conversation",
        "action_query": None,
    }
    if decision.action == "continue":
        request = decision.user_request.strip()
        # Keep analysis records and active evidence until routing decides to start
        # a new analysis. ensure_analysis_task owns the run-scoped reset.
        from uuid import uuid4
        message_id = str(uuid4())
        update.update({
            "user_request": request,
            "routing_context": "main",
            "approval_feedback": None,
            "final_response": {},
            "referenced_analysis_task_ids": [],
            "analysis_reference_ambiguous": False,
            "last_received_message_id": message_id,
            "messages": [{"role": "user", "content": request, "id": message_id}],
        })
        # TODO(CRUD): 사용자 메시지 POST
    return update
