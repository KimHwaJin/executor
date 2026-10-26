"""Authoring contract for Workflow 1.0 and its separate execution inputs."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Name = Annotated[str, Field(min_length=1)]
Index = Annotated[int, Field(ge=0)]


class LiteralArgument(StrictModel):
    source: Literal["literal"]
    value: Any


class InputArgument(StrictModel):
    source: Literal["input"]


class OutputArgument(StrictModel):
    source: Literal["tool_output"]
    step_index: Index
    tool_index: Index
    output: Name


Argument = Annotated[
    LiteralArgument | InputArgument | OutputArgument, Field(discriminator="source")
]


class StandardTool(StrictModel):
    tool: Name
    execution: Literal["always", "conditional"]
    arguments: dict[Name, Argument] = Field(default_factory=dict)


class StandardStep(StrictModel):
    skill: Name
    tools: list[StandardTool] = Field(min_length=1)


class Actor(StrictModel):
    type: Literal["USER", "AGENT"]
    id: Name


class StandardDefinition(StrictModel):
    actor: Actor
    name: Name
    user_request: Name
    execution_mode: Literal["static", "adaptive"]
    allow_immediate_execution: bool
    steps: list[StandardStep] = Field(min_length=1)

    @model_validator(mode="after")
    def check_execution_mode(self):
        conditional = any(t.execution == "conditional" for s in self.steps for t in s.tools)
        if conditional != (self.execution_mode == "adaptive"):
            raise ValueError("execution_mode must match the presence of conditional calls")
        return self


class StandardWorkflow(StrictModel):
    workflow_version: Literal["1.0"]
    workflow: StandardDefinition


class ExecutionInput(StrictModel):
    """One value supplied for a particular call; never part of the reusable definition."""

    step_index: Index
    tool_index: Index
    parameter: Name
    value: Any
    origin: Literal["user_request", "user_answer"] = "user_answer"
    confirmed: bool = True
