"""Public workflow execution contract, independent of the graph transport."""
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextContent(StrictModel):
    type: Literal["text"]
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class WorkflowInput(StrictModel):
    content: list[TextContent] = Field(min_length=1)


class ResumeValue(StrictModel):
    action: str = Field(min_length=1)
    interaction_id: UUID
    revision: int = Field(ge=1, strict=True)
    feedback: str = ""
    # Form-based interactions can carry structured answers without JSON-in-text.
    payload: dict[str, Any] | None = None


class ResumeCommand(StrictModel):
    resume: ResumeValue


class WorkflowRunRequest(StrictModel):
    input: WorkflowInput | None = None
    session_id: UUID | None = None
    project_id: UUID | None = None
    task_id: UUID | None = None
    run_id: UUID | None = None
    command: ResumeCommand | None = None

    @model_validator(mode="after")
    def validate_operation(self):
        if (self.input is None) == (self.command is None):
            raise ValueError("Exactly one of input or command is required")
        if self.command is not None:
            if self.task_id is None or self.run_id is None:
                raise ValueError("Resume requires task_id and run_id")
            if self.session_id is not None or self.project_id is not None:
                raise ValueError("Resume resolves the session from run_id")
        elif self.task_id is not None or self.run_id is not None:
            raise ValueError("New input must not specify task_id or run_id")
        return self
