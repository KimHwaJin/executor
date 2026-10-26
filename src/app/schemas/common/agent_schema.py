from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import AgentRunStatus


AgentWorkflowStage = Literal[
    "prepared",
    "dispatched",
    "agent_completed",
    "redis_received",
    "interpreting",
    "completed",
    "failed",
]


class AgentMessage(BaseModel):
    role: str
    content: str
    message_id: UUID | None = None


class AgentRequestEnvelope(BaseModel):
    schema_version: str = "1.0"
    run_id: UUID
    session_id: UUID
    trigger_message_id: UUID
    messages: list[AgentMessage] = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    redis_result_key: str = Field(min_length=1, max_length=500)


class AgentResultEnvelope(BaseModel):
    schema_version: str = "1.0"
    run_id: UUID
    status: Literal["success", "error"]
    output: dict[str, Any] = Field(default_factory=dict)
    message_text: str | None = None
    error: dict[str, Any] | None = None


class RedisAgentResult(BaseModel):
    run_id: UUID
    result: dict[str, Any]
    produced_at: datetime


class AgentWorkflowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: UUID
    session_id: UUID
    trigger_message_id: UUID | None
    agent_message_id: UUID | None
    interpreted_message_id: UUID | None
    status: AgentRunStatus
    workflow_stage: str
    request_payload: dict[str, Any] | None
    agent_response: dict[str, Any] | None
    redis_key: str | None
    redis_result: dict[str, Any] | None
    created_at: datetime
    dispatched_at: datetime | None
    agent_completed_at: datetime | None
    redis_received_at: datetime | None
    interpreted_at: datetime | None
    completed_at: datetime | None

