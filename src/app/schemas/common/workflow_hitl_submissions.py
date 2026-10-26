"""Shared submission models; re-exported by workflow_hitl_schema for compatibility."""
from typing import Annotated, Any, Literal
from pydantic import Field, StringConstraints
from app.schemas.common.workflow_run_schema import StrictModel

Nonblank = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


class CandidateSelection(StrictModel):
    intent: Literal["select"]
    candidate_id: Nonblank


class CandidateRetry(StrictModel):
    intent: Literal["recommend_again", "regenerate"]
    feedback: str = ""


class CandidateGeneration(StrictModel):
    intent: Literal["generate"]


class ReasonRejection(StrictModel):
    intent: Literal["reject"]
    reason: Nonblank


class ToolInputValue(StrictModel):
    step_index: int = Field(ge=0, strict=True)
    tool_index: int = Field(ge=0, strict=True)
    parameter: Nonblank
    value: Any


class ToolOutputReference(StrictModel):
    step_index: int = Field(ge=0, strict=True)
    tool_index: int = Field(ge=0, strict=True)
    output: Nonblank


class ToolValueSubmission(ToolInputValue):
    source: Literal["value"] = "value"


class ToolOutputSubmission(StrictModel):
    step_index: int = Field(ge=0, strict=True)
    tool_index: int = Field(ge=0, strict=True)
    parameter: Nonblank
    source: Literal["tool_output"]
    reference: ToolOutputReference


class ToolDefaultSubmission(StrictModel):
    step_index: int = Field(ge=0, strict=True)
    tool_index: int = Field(ge=0, strict=True)
    parameter: Nonblank
    source: Literal["default"]


class ToolInputSubmission(StrictModel):
    intent: Literal["submit_inputs"]
    workflow_id: Nonblank
    workflow_revision: int = Field(ge=1, strict=True)
    inputs: list[ToolValueSubmission | ToolOutputSubmission | ToolDefaultSubmission]

