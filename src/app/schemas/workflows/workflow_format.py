"""Registry-only Workflow structured-output contract."""

from __future__ import annotations

import keyword
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowStatus(StrEnum):
    DRAFT = "draft"
    NEEDS_INPUT = "needs_input"
    READY = "ready"


class WorkflowExecutionMode(StrEnum):
    STATIC = "static"
    ADAPTIVE = "adaptive"


class ExecutionMode(StrEnum):
    ALWAYS = "always"
    CONDITIONAL = "conditional"



class ArgumentSource(StrEnum):
    WORKFLOW_INPUT = "workflow_input"
    STEP_OUTPUT = "step_output"
    CONTEXT = "context"
    DEFAULT = "default"
    PLANNER = "planner"


class InputDefinition(StrictModel):
    type: str = Field(min_length=1)
    required: bool = True
    allow_llm_inference: bool
    validation: list[dict[str, Any]] = Field(default_factory=list)


class InputProvenance(StrictModel):
    source: Literal[
        "user_query",
        "user_answer",
        "confirmed_metadata",
        "external_context",
    ] | None
    confirmed: bool


class UnresolvedInput(StrictModel):
    name: str = Field(min_length=1)
    question: str = Field(min_length=1)
    required_for: list[str] = Field(default_factory=list)


class RuntimeDecisionCondition(StrictModel):
    type: Literal["runtime_decision"]
    decision_id: str = Field(min_length=1)


WorkflowCondition = RuntimeDecisionCondition


class OutputBinding(StrictModel):
    selector: str = Field(min_length=1)
    variable: str = Field(
        min_length=1,
        description=(
            "Workflow 전체에서 고유한 Notebook 출력 변수명. selector가 '$'가 "
            "아니면 Tool의 result_variable과 달라야 한다."
        ),
    )

    @model_validator(mode="after")
    def validate_variable(self) -> "OutputBinding":
        if not self.variable.isidentifier() or keyword.iskeyword(self.variable):
            raise ValueError(f"유효하지 않은 Python 변수명입니다: {self.variable!r}")
        return self


class ToolReturns(StrictModel):
    result_variable: str = Field(
        min_length=1,
        description=(
            "함수 전체 반환값을 저장하는 Workflow 전체 고유 임시 변수명. "
            "필드 selector를 사용하는 output variable과 달라야 한다."
        ),
    )
    outputs: dict[str, OutputBinding] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_result_variable(self) -> "ToolReturns":
        if not self.result_variable.isidentifier() or keyword.iskeyword(
            self.result_variable
        ):
            raise ValueError("result_variable은 유효한 Python 변수명이어야 합니다.")
        return self


class WorkflowTool(StrictModel):
    id: str = Field(min_length=1)
    order: int = Field(gt=0)
    tool: str = Field(min_length=1)
    tool_origin: Literal["registry"] = "registry"
    tool_source: str = Field(min_length=1)
    selection_reason: str = Field(min_length=1)
    execution: ExecutionMode
    condition: WorkflowCondition | None = None
    arguments: dict[str, Any]
    argument_sources: dict[str, ArgumentSource]
    returns: ToolReturns

    @model_validator(mode="after")
    def validate_tool(self) -> "WorkflowTool":
        if set(self.arguments) != set(self.argument_sources):
            raise ValueError(
                "arguments와 argument_sources의 key가 정확히 일치해야 합니다."
            )
        if self.execution == ExecutionMode.ALWAYS and self.condition is not None:
            raise ValueError("always Tool에는 condition을 지정할 수 없습니다.")
        return self


class WorkflowStep(StrictModel):
    id: str = Field(min_length=1)
    order: int = Field(gt=0)
    skill: str = Field(min_length=1)
    skill_source: str = Field(min_length=1)
    depends_on: list[str]
    execution: ExecutionMode
    condition: WorkflowCondition | None = None
    tools: list[WorkflowTool] = Field(min_length=1)
    outputs: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_step(self) -> "WorkflowStep":
        if self.execution == ExecutionMode.ALWAYS and self.condition is not None:
            raise ValueError("always Step에는 condition을 지정할 수 없습니다.")
        tool_ids = [tool.id for tool in self.tools]
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError(f"Step {self.id!r}의 Tool id가 중복됩니다.")
        tool_orders = [tool.order for tool in self.tools]
        if len(tool_orders) != len(set(tool_orders)):
            raise ValueError(f"Step {self.id!r}의 Tool order가 중복됩니다.")
        return self


class WorkflowDefinition(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    status: WorkflowStatus
    execution_mode: WorkflowExecutionMode = WorkflowExecutionMode.STATIC
    input_schema: dict[str, InputDefinition]
    inputs: dict[str, Any]
    input_provenance: dict[str, InputProvenance]
    unresolved_inputs: list[UnresolvedInput]
    context: dict[str, Any] = Field(default_factory=dict)
    steps: list[WorkflowStep] = Field(min_length=1)
    outputs: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_workflow(self) -> "WorkflowDefinition":
        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Step id가 중복됩니다.")
        step_orders = [step.order for step in self.steps]
        if len(step_orders) != len(set(step_orders)):
            raise ValueError("Step order가 중복됩니다.")

        tools = [tool for step in self.steps for tool in step.tools]
        tool_ids = [tool.id for tool in tools]
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError("Workflow 전체에서 Tool id가 중복됩니다.")
        runtime_conditions: list[RuntimeDecisionCondition] = []
        for step in self.steps:
            if isinstance(step.condition, RuntimeDecisionCondition):
                runtime_conditions.append(step.condition)
            for tool in step.tools:
                if isinstance(tool.condition, RuntimeDecisionCondition):
                    runtime_conditions.append(tool.condition)

        if runtime_conditions:
            raise ValueError(
                "Workflow definition에는 Tool/Step condition을 작성하지 않습니다. "
                "조건 판단은 skill_index와 execution run state에서 관리합니다."
            )

        has_conditional_execution = any(
            step.execution == ExecutionMode.CONDITIONAL
            or any(
                tool.execution == ExecutionMode.CONDITIONAL
                for tool in step.tools
            )
            for step in self.steps
        )
        if self.execution_mode == WorkflowExecutionMode.STATIC:
            if has_conditional_execution:
                raise ValueError(
                    "static Workflow의 모든 Step과 Tool은 always여야 합니다."
                )
        elif not has_conditional_execution:
            raise ValueError(
                "adaptive Workflow에는 conditional Step 또는 Tool이 필요합니다."
            )

        if self.status == WorkflowStatus.READY:
            if self.unresolved_inputs:
                raise ValueError("ready Workflow에는 unresolved_inputs가 없어야 합니다.")
            for name, definition in self.input_schema.items():
                if not definition.required:
                    continue
                if name not in self.inputs or self.inputs[name] is None:
                    raise ValueError(f"필수 Workflow input이 없습니다: {name}")
                provenance = self.input_provenance.get(name)
                if provenance is None or not provenance.confirmed:
                    raise ValueError(f"필수 Workflow input이 확정되지 않았습니다: {name}")
        elif self.status == WorkflowStatus.NEEDS_INPUT and not self.unresolved_inputs:
            raise ValueError("needs_input Workflow에는 unresolved_inputs가 필요합니다.")

        used_variables: set[str] = set()
        for tool in tools:
            result_variable = tool.returns.result_variable
            if result_variable in used_variables:
                raise ValueError(f"Notebook 변수가 중복됩니다: {result_variable}")
            used_variables.add(result_variable)
            for output in tool.returns.outputs.values():
                if output.selector == "$" and output.variable == result_variable:
                    continue
                if output.variable in used_variables:
                    raise ValueError(f"Notebook 변수가 중복됩니다: {output.variable}")
                used_variables.add(output.variable)
        return self


class WorkflowGeneratorOutput(StrictModel):
    schema_version: Literal["1.3"]
    workflow: WorkflowDefinition


__all__ = [
    "ArgumentSource",
    "ExecutionMode",
    "InputDefinition",
    "InputProvenance",
    "OutputBinding",
    "RuntimeDecisionCondition",
    "ToolReturns",
    "UnresolvedInput",
    "WorkflowDefinition",
    "WorkflowExecutionMode",
    "WorkflowGeneratorOutput",
    "WorkflowStatus",
    "WorkflowStep",
    "WorkflowTool",
]
