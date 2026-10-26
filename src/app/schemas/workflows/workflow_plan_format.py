"""Compact semantic Workflow plan authored by the LLM."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.workflows.workflow_format import (
    InputDefinition,
    InputProvenance,
    UnresolvedInput,
    WorkflowStatus,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanArgument(StrictModel):
    source: Literal[
        "workflow_input",
        "step_output",
        "context",
        "default",
        "planner",
    ]
    value: Any = None
    input_name: str | None = None
    context_name: str | None = None
    step_id: str | None = None
    tool: str | None = None
    output: str | None = None

    @model_validator(mode="after")
    def validate_source_fields(self) -> "PlanArgument":
        if self.source == "workflow_input" and not self.input_name:
            raise ValueError("workflow_input argument requires input_name")
        if self.source == "context" and not self.context_name:
            raise ValueError("context argument requires context_name")
        if self.source == "step_output" and (not self.step_id or not self.output):
            raise ValueError("step_output argument requires step_id and output")
        return self


class PlanOutputReference(StrictModel):
    step_id: str = Field(min_length=1)
    tool: str | None = None
    output: str = Field(min_length=1)


class PlanTool(StrictModel):
    tool: str = Field(min_length=1)
    selection_reason: str = Field(min_length=1)
    arguments: dict[str, PlanArgument] = Field(default_factory=dict)


class PlanStep(StrictModel):
    id: str = Field(min_length=1)
    skill: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    tools: list[PlanTool] = Field(min_length=1)


class WorkflowPlanDefinition(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    status: WorkflowStatus
    input_schema: dict[str, InputDefinition] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)
    input_provenance: dict[str, InputProvenance] = Field(default_factory=dict)
    unresolved_inputs: list[UnresolvedInput] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    steps: list[PlanStep] = Field(min_length=1)
    outputs: dict[str, PlanOutputReference] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_plan(self) -> "WorkflowPlanDefinition":
        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Plan Step id가 중복됩니다.")
        known: set[str] = set()
        for step in self.steps:
            missing = {
                dependency
                for dependency in set(step.depends_on) - known
                if not (
                    dependency.startswith("load_data_")
                    and dependency.removeprefix("load_data_").isdigit()
                    and int(dependency.removeprefix("load_data_")) > 0
                )
            }
            if missing:
                raise ValueError(
                    f"Plan Step {step.id!r} has unknown/forward dependencies: "
                    f"{sorted(missing)}"
                )
            known.add(step.id)
        return self


class WorkflowPlanOutput(StrictModel):
    plan_version: Literal["1.0"]
    workflow: WorkflowPlanDefinition


__all__ = [
    "PlanArgument",
    "PlanOutputReference",
    "PlanStep",
    "PlanTool",
    "WorkflowPlanDefinition",
    "WorkflowPlanOutput",
]
