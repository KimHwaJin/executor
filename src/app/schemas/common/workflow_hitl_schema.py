"""Shared HITL v2 display/submission models and dynamic submission schemas."""
from copy import deepcopy
from typing import Any, Literal

from jsonschema import Draft202012Validator
from pydantic import Field, model_serializer, model_validator

from app.schemas.common.workflow_run_schema import StrictModel

from app.schemas.common.workflow_hitl_submissions import (
    Nonblank, CandidateSelection, CandidateRetry, CandidateGeneration,
    ReasonRejection, ToolInputValue, ToolInputSubmission,
    ToolOutputReference, ToolValueSubmission, ToolOutputSubmission, ToolDefaultSubmission,
)
from app.schemas.common.workflow_hitl_actions import (
    CANDIDATE_SUBMISSION, action_intents, get_action,
)


class AvailableAction(StrictModel):
    intent: Nonblank
    label: Nonblank


def _check_actions(actions, allowed):
    intents = [a.intent for a in actions]
    if len(intents) != len(set(intents)) or not set(intents) <= allowed:
        raise ValueError("Duplicate or unsupported available action")
    return set(intents)


class WorkflowCandidate(StrictModel):
    candidate_id: Nonblank
    origin: Literal["recommended", "generated"]
    name: Nonblank | None = None
    description: str
    reason: str
    workflow_id: str | None = None
    version_id: str | None = None
    revision: int | None = Field(default=None, ge=1)
    similarity_score: float | None = Field(default=None, ge=-1, le=1)
    # Workflow document version/compilation is owned by the workflow domain.
    workflow: dict[str, Any]

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        result = handler(self)
        for key in ("name", "workflow_id", "version_id", "revision", "similarity_score"):
            if getattr(self, key) is None:
                result.pop(key, None)
        return result


class RecommendationSearchResult(StrictModel):
    status: Literal["matched", "no_additional_matches", "no_matches"]
    added_candidate_ids: list[str] = Field(default_factory=list)
    message: str | None = None


class CandidateSelectionDisplay(StrictModel):
    candidates: list[WorkflowCandidate]
    recommendation_search: RecommendationSearchResult | None = None
    available_actions: list[AvailableAction] = Field(min_length=1)

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        result = handler(self)
        if self.recommendation_search is None:
            result.pop("recommendation_search", None)
        return result

    @model_validator(mode="after")
    def valid_candidates(self):
        intents = _check_actions(self.available_actions, action_intents("candidate"))
        ids = [c.candidate_id for c in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate candidate_id")
        if not ids and any(get_action("candidate", intent).requires_candidates for intent in intents):
            raise ValueError("Cannot select from an empty candidate list")
        return self


class ToolOutputOption(ToolOutputReference):
    skill: Nonblank
    tool: Nonblank
    description: str = ""
    type: str | None = None


class ToolParameterDisplay(StrictModel):
    name: Nonblank
    title: str
    description: str
    required: bool
    editable: bool
    value_schema: dict[str, Any]
    has_default: bool
    default_value: Any = None
    has_value: bool
    value: Any = None
    value_origin: Literal["user_request", "default", "user_answer", "unset", "workflow_literal", "tool_output"]
    binding: dict[str, Any] | None = None
    output_options: list[ToolOutputOption] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_values(self):
        for flag, field in ((self.has_default, "default_value"), (self.has_value, "value")):
            if flag != (field in self.model_fields_set):
                raise ValueError(f"{field} presence must match its has_* flag")
        if not self.required and not self.has_default:
            raise ValueError("Optional parameters require a default, including null")
        if self.value_origin == "tool_output" and self.binding is None:
            raise ValueError("Tool output bindings must provide binding")
        if self.value_origin == "unset" and self.has_value:
            raise ValueError("Unset parameters cannot have a value")
        Draft202012Validator.check_schema(self.value_schema)
        validator = Draft202012Validator(self.value_schema)
        for flag, value in ((self.has_default, self.default_value), (self.has_value, self.value)):
            if flag and not validator.is_valid(value):
                raise ValueError("Displayed/default value violates value_schema")
        return self

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        result = handler(self)
        if not self.has_default:
            result.pop("default_value", None)
        if not self.has_value:
            result.pop("value", None)
        if self.binding is None:
            result.pop("binding", None)
        if "output_options" not in self.model_fields_set:
            result.pop("output_options", None)
        return result


class ToolCallDisplay(StrictModel):
    step_index: int = Field(ge=0, strict=True)
    skill: Nonblank
    tool_index: int = Field(ge=0, strict=True)
    tool: Nonblank
    parameters: list[ToolParameterDisplay]

    @model_validator(mode="after")
    def unique_parameters(self):
        names = [p.name for p in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate parameter in tool call")
        return self


class CatalogOutput(StrictModel):
    name: Nonblank
    type: str | None = None
    description: str = ""
    selectable: bool


class CatalogTool(StrictModel):
    tool_index: int = Field(ge=0, strict=True)
    tool: Nonblank
    outputs: list[CatalogOutput]


class CatalogStep(StrictModel):
    step_index: int = Field(ge=0, strict=True)
    skill: Nonblank
    tools: list[CatalogTool]


class ToolInputDisplay(StrictModel):
    workflow_id: Nonblank
    workflow_revision: int = Field(ge=1, strict=True)
    workflow: dict[str, Any]
    tool_inputs: list[ToolCallDisplay]
    output_catalog: list[CatalogStep] = Field(default_factory=list)
    available_actions: list[AvailableAction] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_tools(self):
        catalog_addresses = [(s.step_index, t.tool_index, o.name)
                             for s in self.output_catalog for t in s.tools for o in t.outputs]
        if len(catalog_addresses) != len(set(catalog_addresses)):
            raise ValueError("Duplicate output catalog address")
        _check_actions(self.available_actions, action_intents("tool_inputs"))
        addresses = [(t.step_index, t.tool_index) for t in self.tool_inputs]
        if len(addresses) != len(set(addresses)):
            raise ValueError("Duplicate tool call address")
        return self

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        result = handler(self)
        if "output_catalog" not in self.model_fields_set:
            result.pop("output_catalog", None)
        return result


class PlanReviewDisplay(StrictModel):
    workflow_id: Nonblank
    workflow_revision: int = Field(ge=1, strict=True)
    workflow: dict[str, Any]
    execution_inputs: list[ToolInputValue]
    available_actions: list[AvailableAction] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_plan(self):
        _check_actions(self.available_actions, action_intents("plan"))
        addresses = [(i.step_index, i.tool_index, i.parameter) for i in self.execution_inputs]
        if len(addresses) != len(set(addresses)):
            raise ValueError("Duplicate execution input address")
        return self


def build_candidate_submission_schema(display: CandidateSelectionDisplay) -> dict[str, Any]:
    """Only offer intents/IDs present in this immutable display snapshot."""
    display = CandidateSelectionDisplay.model_validate(display.model_dump(mode="json"))
    branches = [get_action("candidate", action.intent).schema(
        candidate_ids=[c.candidate_id for c in display.candidates],
    ) for action in display.available_actions]
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "oneOf": branches}


def build_tool_input_submission_schema(display: ToolInputDisplay) -> dict[str, Any]:
    """Constrain per-call values, required inputs and unique input addresses."""
    display = ToolInputDisplay.model_validate(display.model_dump(mode="json"))
    schema = ToolInputSubmission.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    catalog_mode = "output_catalog" in display.model_fields_set
    schema["properties"]["workflow_id"]["const"] = display.workflow_id
    schema["properties"]["workflow_revision"]["const"] = display.workflow_revision
    branches, constraints = [], []
    for tool in display.tool_inputs:
        for parameter in tool.parameters:
            if not parameter.editable:
                continue
            item = ToolValueSubmission.model_json_schema()
            address = {"step_index": tool.step_index, "tool_index": tool.tool_index, "parameter": parameter.name}
            for key, value in address.items():
                item["properties"][key]["const"] = value
            # Give nested $defs/#/$ref their own resource scope under this schema.
            value_schema = deepcopy(parameter.value_schema)
            value_schema.setdefault("$id", f"urn:workflow-hitl:parameter:{len(branches)}")
            item["properties"]["value"] = value_schema
            branches.append(item)
            if parameter.has_default:
                default = ToolDefaultSubmission.model_json_schema()
                for key, value in address.items():
                    default["properties"][key]["const"] = value
                branches.append(default)
            if catalog_mode or parameter.output_options:
                ref = ToolOutputSubmission.model_json_schema()
                ref.pop("$defs", None)
                for key, value in address.items():
                    ref["properties"][key]["const"] = value
                if catalog_mode:
                    ref["properties"]["reference"] = {"$ref": "#/$defs/ToolOutputReference"}
                else:
                    ref["properties"]["reference"] = {
                        **ToolOutputReference.model_json_schema(),
                        "enum": [o.model_dump(include={"step_index", "tool_index", "output"})
                                 for o in parameter.output_options],
                    }
                branches.append(ref)
            constraints.append({
                "contains": {"type": "object", "properties": {k: {"const": v} for k, v in address.items()},
                             "required": list(address)},
                "minContains": 1 if parameter.required and parameter.binding is None else 0, "maxContains": 1,
            })
    array = {"type": "array", "items": {"oneOf": branches} if branches else False,
             "maxItems": len(constraints)}
    if constraints:
        array["allOf"] = constraints
    schema["properties"]["inputs"] = array
    return schema


def build_plan_review_submission_schema(display: PlanReviewDisplay) -> dict[str, Any]:
    """Schema for edit/reason only. Approve has no payload."""
    display = PlanReviewDisplay.model_validate(display.model_dump(mode="json"))
    if "reject" not in {a.intent for a in display.available_actions}:
        # No edit decision should be offered for an approve-only display.
        return {"not": {}}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **ReasonRejection.model_json_schema()}
