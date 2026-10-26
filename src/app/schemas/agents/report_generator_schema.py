"""Contracts for analysis report generation from execution results."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutionStepResult(StrictModel):
    execution_id: str = Field(min_length=1)
    task_id: str | None = None
    sequence: int | None = None
    skill: str | None = None
    step_id: str | None = None
    tool: str | None = None
    tool_id: str | None = None
    code_path: str | None = None
    status: str = Field(min_length=1)
    message: str | None = None
    event_id: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class ReportGenerationRequest(StrictModel):
    user_request: str = ""
    analysis_intent: dict[str, Any] = Field(default_factory=dict)
    workflow: dict[str, Any] = Field(default_factory=dict)
    task_id: str = Field(min_length=1)
    execution_id: str | None = None
    step_results: list[ExecutionStepResult] = Field(default_factory=list)


class AnalysisReportOutput(StrictModel):
    content: str = Field(min_length=1)


__all__ = [
    "AnalysisReportOutput",
    "ExecutionStepResult",
    "ReportGenerationRequest",
]
