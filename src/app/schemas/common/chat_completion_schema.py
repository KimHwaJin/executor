"""Agent conversation operations share POST /chat/completion, including SSE subscription."""
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common.message_schema import MessageCreate


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Message(Operation):
    type: Literal["message"]
    session_id: UUID
    content_text: str = Field(min_length=1)

    @field_validator("content_text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("content_text must not be blank")
        return value

    # Reuse the same whitespace validation as the message CRUD contract.
    def message_payload(self, **kwargs) -> MessageCreate:
        return MessageCreate(session_id=self.session_id, content_text=self.content_text, **kwargs)


class Resume(Operation):
    type: Literal["resume"]
    session_id: UUID
    run_id: UUID
    command: dict[str, Any] | str


class Status(Operation):
    type: Literal["status"]
    session_id: UUID
    run_id: UUID


class Stream(Operation):
    type: Literal["stream"]
    task_id: UUID
    after: int = Field(default=0, ge=0)


class Cancel(Operation):
    type: Literal["cancel"]
    task_id: UUID
    reason: str | None = Field(default=None, max_length=1000)


ChatCompletionRequest = Annotated[
    Message | Resume | Status | Stream | Cancel, Field(discriminator="type"),
]
