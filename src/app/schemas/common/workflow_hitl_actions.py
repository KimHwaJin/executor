"""Single registry for HITL actions, presentation, transport and dispatch.

Submission model definitions are in workflow_hitl_submissions.py. A new action
is registered here; graph edges are still required for entirely new destinations.
"""
from dataclasses import dataclass, field
from typing import Annotated, Any, Callable, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

from app.schemas.common.workflow_hitl_submissions import (
    CandidateSelection, CandidateRetry, CandidateGeneration, ReasonRejection,
    ToolInputSubmission,
)


@dataclass(frozen=True)
class CandidateActionResult:
    updates: dict[str, Any] = field(default_factory=dict)
    selected_candidate_id: str | None = None


def _select(state, answer):
    return CandidateActionResult(selected_candidate_id=answer.candidate_id)


def _generate(state, answer):
    return CandidateActionResult(updates={
        "approval_feedback": None, "previous_workflow_definition": {},
        "routing_context": "main", "approval": {},
        "skip_workflow_recommendation": True,
    })


def _regenerate(state, answer):
    # Late import avoids coupling schema/API import to the graph at startup.
    from app.graphs.nodes.candidate_contract import excluded_candidates
    return CandidateActionResult(updates={
        "excluded_workflow_candidates": excluded_candidates(state, state.get("workflow_candidates", [])),
        "approval_feedback": answer.feedback or None, "approval": {},
        "skip_workflow_recommendation": True,
    })


def _recommend_again(state, answer):
    return CandidateActionResult(updates={"append_workflow_recommendations": True, "approval": {}})


def _reject(state, answer):
    return CandidateActionResult(updates={
        "approval_feedback": answer.reason, "user_request": answer.reason,
        "routing_context": "workflow_rejected", "approval": {},
    })


@dataclass(frozen=True)
class HitlActionSpec:
    scope: Literal["candidate", "tool_inputs", "plan"]
    intent: str
    label: str
    submission_model: type[BaseModel] | None
    transport_action: Literal["edit", "approve"] = "edit"
    origins: tuple[str, ...] = ()
    capability: str | None = None
    requires_candidates: bool = False
    candidate_id_field: str | None = None
    handler: Callable[[dict, BaseModel], CandidateActionResult] | None = None
    destination: str | None = None

    def schema(self, *, candidate_ids: list[str] | None = None) -> dict:
        if self.submission_model is None:
            return {"type": "object", "additionalProperties": False}
        schema = self.submission_model.model_json_schema()
        schema["properties"]["intent"] = {"type": "string", "const": self.intent}
        if self.candidate_id_field and candidate_ids is not None:
            schema["properties"][self.candidate_id_field]["enum"] = list(candidate_ids)
        return schema


# Display order is declaration order. Origins/capability control visibility;
# available_actions still contains only {intent, label}, preserving the API.
ACTIONS = (
    HitlActionSpec("candidate", "select", "선택", CandidateSelection,
                   requires_candidates=True, candidate_id_field="candidate_id", handler=_select),
    HitlActionSpec("candidate", "recommend_again", "다시 추천", CandidateRetry,
                   origins=("recommended",), capability="recommend_again", handler=_recommend_again, destination="recommend_workflow"),
    HitlActionSpec("candidate", "generate", "워크플로우 생성하기", CandidateGeneration,
                   origins=("recommended",), capability="generate", handler=_generate,
                   destination="announce_workflow_search"),
    HitlActionSpec("candidate", "regenerate", "다시 생성", CandidateRetry,
                   origins=("generated",), capability="regenerate", handler=_regenerate,
                   destination="announce_workflow_search"),
    HitlActionSpec("candidate", "reject", "이유와 함께 거절", ReasonRejection,
                   handler=_reject, destination="route_user_request"),
    HitlActionSpec("tool_inputs", "submit_inputs", "입력 제출", ToolInputSubmission),
    HitlActionSpec("plan", "approve", "승인", None, transport_action="approve"),
    HitlActionSpec("plan", "reject", "이유와 함께 거절", ReasonRejection),
)


def action_intents(scope: str) -> set[str]:
    return {a.intent for a in ACTIONS if a.scope == scope}


def get_action(scope: str, intent: str) -> HitlActionSpec:
    for action in ACTIONS:
        if (action.scope, action.intent) == (scope, intent):
            return action
    raise ValueError(f"Unknown HITL action: {scope}/{intent}")


def candidate_actions(*, origin: str | None, has_candidates: bool, capabilities: dict[str, bool]) -> list[dict]:
    return [{"intent": a.intent, "label": a.label} for a in ACTIONS
            if a.scope == "candidate"
            and (not a.origins or origin in a.origins)
            and (not a.requires_candidates or has_candidates)
            and (a.capability is None or capabilities.get(a.capability, False))]


def transport_actions(scope: str, intents: list[str]) -> list[str]:
    return list(dict.fromkeys(get_action(scope, intent).transport_action for intent in intents))


def dispatch_candidate(state: dict, answer: BaseModel) -> CandidateActionResult:
    action = get_action("candidate", answer.intent)
    if action.handler is None:
        raise ValueError(f"Candidate action is not connected: {answer.intent}")
    return action.handler(state, answer)


# Derive the API/agent validation union from the same registration list.
_candidate_models = tuple(dict.fromkeys(a.submission_model for a in ACTIONS if a.scope == "candidate"))
CANDIDATE_SUBMISSION = TypeAdapter(Annotated[Union[_candidate_models], Field(discriminator="intent")])
