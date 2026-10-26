"""Versioned frontend Task contract; worker leases remain internal runtime state."""
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.enums import TaskStatus
from app.schemas.common.run_schema import RunResource


class ResumeTarget(BaseModel):
    task_id: UUID
    run_id: UUID
    interaction_id: UUID
    revision: int = Field(ge=1)


class InteractionAction(BaseModel):
    name: str
    label: str
    input_schema: dict[str, Any] = Field(default_factory=dict)


class FormPayload(BaseModel):
    action_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)


class InteractionResource(BaseModel):
    interaction_id: UUID
    revision: int
    run_id: UUID
    status: Literal["open"] = "open"
    kind: str
    payload_version: int = 1
    summary: str
    payload: FormPayload
    actions: list[InteractionAction]
    resume_target: ResumeTarget


class Capabilities(BaseModel):
    can_cancel: bool
    can_resume: bool


class RecoveryState(BaseModel):
    status: Literal["none", "retry_scheduled", "failed"] = "none"
    next_attempt_at: datetime | None = None
    user_action_required: bool = False


class TaskSnapshot(BaseModel):
    schema_version: int = 1
    task_id: UUID
    session_id: UUID
    current_run_id: UUID | None
    # On POST, this identifies the accepted/replayed submission even if the Task advanced.
    submission_run_id: UUID | None = None
    status: TaskStatus
    phase: str
    last_event_sequence: int
    input_required: bool
    task_finished: bool
    current_run: RunResource | None
    interactions: list[InteractionResource] = Field(default_factory=list)
    capabilities: Capabilities
    result: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    recovery: RecoveryState
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class TaskEventEnvelope(BaseModel):
    schema_version: int = 1
    event_id: UUID | None = None
    type: str
    sequence: int
    session_id: UUID
    task_id: UUID
    run_id: UUID | None
    occurred_at: datetime
    data: dict[str, Any]
