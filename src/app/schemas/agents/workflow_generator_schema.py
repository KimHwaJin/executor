"""Input contract for the Workflow Generator agent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from app.schemas.common.dataset_types import DatasetType, normalize_dataset_type

from ..workflows.workflow_standard import ExecutionInput, StandardWorkflow


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatasetRequirement(StrictModel):
    """One source dataset needed to fulfill the request, before planning calls."""

    data_type: DatasetType
    role: Literal["features", "target", "analysis"]

    @field_validator("data_type", mode="before")
    @classmethod
    def normalize_type(cls, value):
        return normalize_dataset_type(value)


class SkillSelectionOutput(StrictModel):
    """Complete set of Skill names selected before resource loading."""

    skill_names: list[str] = Field(min_length=1)
    dataset_requirements: list[DatasetRequirement] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_skill_names(self) -> "SkillSelectionOutput":
        if len(self.skill_names) != len(set(self.skill_names)):
            raise ValueError("skill_names must not contain duplicates")
        return self


class WorkflowGenerationOutput(StandardWorkflow):
    """Reusable definition plus request-specific input suggestions for review."""

    execution_inputs: list[ExecutionInput] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_standard_definition(cls, value):
        if isinstance(value, StandardWorkflow) and not isinstance(value, cls):
            return value.model_dump(mode="json")
        return value


__all__ = ["DatasetRequirement", "SkillSelectionOutput", "WorkflowGenerationOutput"]
