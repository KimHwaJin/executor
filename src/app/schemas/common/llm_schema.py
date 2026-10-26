from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import LLMRunStatus


class LLMChatMessage(BaseModel):
    role: str
    content: str


class LLMCompletionRequest(BaseModel):
    model: str
    messages: list[LLMChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None


class LLMCompletionResult(BaseModel):
    content: str = Field(min_length=1)
    raw_response: dict[str, Any]
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    finish_reason: str | None = None


class LLMRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: UUID
    session_id: UUID
    trigger_message_id: UUID
    assistant_message_id: UUID | None
    provider: str
    model_name: str
    status: LLMRunStatus
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    latency_ms: int | None
    finish_reason: str | None
    error_code: str | None
    error_message: str | None
    attempt_count: int
    retry_count: int
    max_retries: int
    attempt_errors: list[dict[str, Any]]
    queued_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

