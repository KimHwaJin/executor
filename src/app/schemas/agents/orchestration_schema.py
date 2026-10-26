"""Structured contracts for the user-agent orchestration graph."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoutingOutput(StrictModel):
    route: Literal[
        "analysis",
        "analysis_question",
        "faq",
        "revise_workflow",
        "cancel",
    ]
    reason: str = Field(min_length=1)


    analysis_task_ids: list[str] = Field(default_factory=list)
    reference_ambiguous: bool = False


class AnalysisIntentOutput(StrictModel):
    intent: Literal["root_cause", "failure_prediction", "data_drift"]
    reason: str = Field(min_length=1)


class RequestContext(StrictModel):
    user_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)


class NextUserRequestResponse(StrictModel):
    action: Literal["continue", "cancel"]
    user_request: str | None = None

    @model_validator(mode="after")
    def validate_continue(self) -> "NextUserRequestResponse":
        if self.action == "continue" and not (self.user_request or "").strip():
            raise ValueError("user_request is required when action=continue")
        return self


class AdditionalInformationResponse(StrictModel):
    answers: dict[str, Any] = Field(min_length=1)


class WorkflowCandidateSelectionResponse(StrictModel):
    approved: bool = True
    selected_candidate_id: str | None = None
    feedback: str | None = None

    @model_validator(mode="after")
    def validate_selection(self) -> "WorkflowCandidateSelectionResponse":
        if self.approved and not (self.selected_candidate_id or "").strip():
            raise ValueError(
                "selected_candidate_id is required when approved=true"
            )
        if not self.approved and not (self.feedback or "").strip():
            raise ValueError("feedback is required when candidates are rejected")
        return self


class ApprovalDecision(StrictModel):
    approved: bool
    feedback: str | None = None

    @model_validator(mode="after")
    def validate_rejection(self) -> "ApprovalDecision":
        if not self.approved and not (self.feedback or "").strip():
            raise ValueError("feedback is required when a workflow is rejected")
        return self


class ConditionalToolDecision(StrictModel):
    tool_id: str = Field(min_length=1)
    decision: Literal["include", "exclude"]
    reason: str = Field(min_length=1)


class ConditionalDecisionOutput(StrictModel):
    decisions: list[ConditionalToolDecision] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_tools(self) -> "ConditionalDecisionOutput":
        tool_ids = [item.tool_id for item in self.decisions]
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError("conditional decision tool_id가 중복됩니다.")
        return self


class PlaceholderResponse(StrictModel):
    status: Literal["placeholder"] = "placeholder"
    message: str = Field(min_length=1)


class FaqOutput(StrictModel):
    answer: str = Field(min_length=1)


class NotebookCell(StrictModel):
    order: int = Field(gt=0)
    id: str = Field(min_length=1)
    cell_type: Literal["code"] = "code"
    role: Literal["tool_execution", "workflow_outputs"]
    skill: str | None = None
    code: str = Field(min_length=1)
    step_id: str | None = None
    tool_id: str | None = None
    tool: str | None = None

    @model_validator(mode="after")
    def validate_execution_metadata(self) -> "NotebookCell":
        if self.role == "tool_execution" and not all(
            (self.skill, self.step_id, self.tool_id, self.tool)
        ):
            raise ValueError(
                "tool_execution cell requires skill, step_id, tool_id, and tool"
            )
        return self


class NotebookGenerationOutput(StrictModel):
    message: str = Field(min_length=1)
    cells: list[NotebookCell] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cells(self) -> "NotebookGenerationOutput":
        orders = [cell.order for cell in self.cells]
        if orders != list(range(1, len(self.cells) + 1)):
            raise ValueError("notebook cell order must be contiguous from 1")
        return self


class ExecutorLifecycle(StrictModel):
    operation_mode: Literal["SINGLE", "MULTI"]
    operation_wait_timeout_seconds: int | None = Field(default=None, gt=0)


class ExecutorActor(StrictModel):
    type: Literal["AGENT"] = "AGENT"
    id: str = Field(min_length=1)


class ExecutorTrigger(StrictModel):
    type: Literal["INTERACTIVE"] = "INTERACTIVE"
    actor: ExecutorActor


class ExecutorRuntime(StrictModel):
    type: Literal["JUPYTER"] = "JUPYTER"
    profile: str = Field(min_length=1)


class ExecutorContext(StrictModel):
    user_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)


class ExecutorPathSource(StrictModel):
    type: Literal["PATH"] = "PATH"
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_source_path(self) -> "ExecutorPathSource":
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts or "\\" in self.path:
            raise ValueError("source.path must be a relative shared-PV path")
        if not self.path.lower().endswith(".py"):
            raise ValueError("source.path must point to a Python file")
        return self


class ExecutorInlineSource(StrictModel):
    type: Literal["INLINE"] = "INLINE"
    content: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_content(self) -> "ExecutorInlineSource":
        if not self.content.strip():
            raise ValueError("source.content must not be blank")
        return self


class ExecutorStepPayload(StrictModel):
    type: Literal["PYTHON_EXECUTE"] = "PYTHON_EXECUTE"
    source: ExecutorPathSource | ExecutorInlineSource


class ExecutorStepLineage(StrictModel):
    skill_name: str | None = None
    tool_name: str | None = None
    input_parameters: dict[str, Any] = Field(default_factory=dict)


class ExecutorSourceStep(StrictModel):
    sequence: int = Field(ge=0)
    payload: ExecutorStepPayload
    step_timeout_seconds: int | None = Field(default=None, gt=0)
    lineage: ExecutorStepLineage


class ExecutorSourceSpec(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    steps: list[ExecutorSourceStep] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_steps(self) -> "ExecutorSourceSpec":
        sequences = [step.sequence for step in self.steps]
        if sequences != list(range(sequences[0], sequences[0] + len(self.steps))):
            raise ValueError("source step sequence must be contiguous")
        paths = [
            step.payload.source.path
            for step in self.steps
            if isinstance(step.payload.source, ExecutorPathSource)
        ]
        if len(paths) != len(set(paths)):
            raise ValueError("payload paths must be unique")
        return self


class ExecutorOperation(StrictModel):
    operation_timeout_seconds: int | None = Field(default=None, gt=0)
    spec: ExecutorSourceSpec
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutorRequestBody(StrictModel):
    idempotency_key: str = Field(min_length=1)
    tenant_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$"
    )
    lifecycle: ExecutorLifecycle
    trigger: ExecutorTrigger
    runtime: ExecutorRuntime
    context: ExecutorContext
    operation: ExecutorOperation

    @model_validator(mode="after")
    def validate_operation_mode(self) -> "ExecutorRequestBody":
        if self.operation.spec.steps[0].sequence != 0:
            raise ValueError("initial Execution Step sequence must start from 0")
        wait_timeout = self.lifecycle.operation_wait_timeout_seconds
        operation_timeout = self.operation.operation_timeout_seconds
        if self.lifecycle.operation_mode == "SINGLE":
            if wait_timeout is not None:
                raise ValueError(
                    "SINGLE executor request must not set operation_wait_timeout_seconds"
                )
        else:
            if wait_timeout is None:
                raise ValueError(
                    "MULTI executor request requires operation_wait_timeout_seconds"
                )
            if operation_timeout is None:
                raise ValueError(
                    "MULTI executor request requires operation_timeout_seconds"
                )
        return self


class ExecutorContinueRequestBody(StrictModel):
    idempotency_key: str = Field(min_length=1)
    expected_version: int = Field(ge=0)
    operation_timeout_seconds: int | None = Field(default=None, gt=0)
    spec: ExecutorSourceSpec
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: ExecutorActor


class ExecutorFinalizeRequestBody(StrictModel):
    idempotency_key: str = Field(min_length=1)
    expected_version: int = Field(ge=0)
    actor: ExecutorActor


class ExecutorCancelRequestBody(StrictModel):
    idempotency_key: str = Field(min_length=1)
    reason: str | None = Field(default=None, max_length=2000)
    actor: ExecutorActor


class ExecutorArtifactInlineSource(StrictModel):
    type: Literal["INLINE"] = "INLINE"
    content: str = Field(min_length=1)


class ExecutorArtifactPathSource(StrictModel):
    type: Literal["PATH"] = "PATH"
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_report_path(self) -> "ExecutorArtifactPathSource":
        path = PurePosixPath(self.path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("artifact source.path must be a safe shared-PV path")
        if path.suffix.lower() != ".md":
            raise ValueError("REPORT artifact source.path must point to Markdown")
        return self


class ExecutorArtifactRequestBody(StrictModel):
    idempotency_key: str = Field(min_length=1)
    type: Literal["REPORT"] = "REPORT"
    source: ExecutorArtifactInlineSource | ExecutorArtifactPathSource
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    media_type: Literal["text/markdown"] = "text/markdown"
    append_to_notebook: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    actor: ExecutorActor


class ExecutorResponseOperationStep(StrictModel):
    sequence: int = Field(ge=0)
    step_id: str = Field(min_length=1)


class ExecutorResponseOperation(StrictModel):
    operation_id: str = Field(min_length=1)
    steps: list[ExecutorResponseOperationStep] = Field(default_factory=list)


class ExecutorResponseState(StrictModel):
    status: Literal[
        "QUEUED",
        "DISPATCHED",
        "RUNNING",
        "WAITING_FOR_OPERATION",
        "FINALIZING",
        "CANCEL_REQUESTED",
        "CANCELLED",
        "SUCCEEDED",
        "FAILED",
    ]
    version: int = Field(ge=0)


class ExecutorSubmitResponse(StrictModel):
    execution_id: str = Field(min_length=1)
    operation: ExecutorResponseOperation
    state: ExecutorResponseState
    created_by_type: Literal["AGENT", "USER", "BATCH"] | None = None
    created_by: str | None = None
    updated_by_type: Literal["AGENT", "USER", "BATCH"] | None = None
    updated_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


__all__ = [
    "AdditionalInformationResponse",
    "AnalysisIntentOutput",
    "ApprovalDecision",
    "ExecutorActor",
    "ExecutorArtifactInlineSource",
    "ExecutorArtifactPathSource",
    "ExecutorArtifactRequestBody",
    "ExecutorContext",
    "ExecutorContinueRequestBody",
    "ExecutorCancelRequestBody",
    "ExecutorFinalizeRequestBody",
    "ExecutorLifecycle",
    "ExecutorInlineSource",
    "ExecutorOperation",
    "ExecutorRequestBody",
    "ExecutorResponseOperation",
    "ExecutorResponseOperationStep",
    "ExecutorResponseState",
    "ExecutorRuntime",
    "ExecutorPathSource",
    "ExecutorSourceSpec",
    "ExecutorSourceStep",
    "ExecutorStepLineage",
    "ExecutorStepPayload",
    "ExecutorSubmitResponse",
    "ExecutorTrigger",
    "FaqOutput",
    "NotebookGenerationOutput",
    "NextUserRequestResponse",
    "PlaceholderResponse",
    "RequestContext",
    "WorkflowCandidateSelectionResponse",
]
