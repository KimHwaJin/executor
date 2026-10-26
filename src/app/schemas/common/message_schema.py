from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.core.enums import DeleteYN, MessageStatus, MessageType
from app.schemas.schema_base import ORMModel
from app.schemas.common.llm_schema import LLMRunRead


class ContentPart(BaseModel):
    type: str
    text: str | None = None
    attachment_id: UUID | None = None

    model_config = {"extra": "allow"}


class MessageCreate(BaseModel):
    # session_id가 없으면 Service가 Session을 먼저 생성합니다.
    session_id: UUID | None = None

    # session_id가 없을 때 생성할 Session의 Project.
    # 이것도 없으면 User의 default Project를 사용합니다.
    project_id: UUID | None = None

    message_type: MessageType = MessageType.USER
    content_text: str = Field(min_length=1)
    content: list[ContentPart] = Field(default_factory=list)
    client_request_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    llm_max_retries: int | None = Field(default=None, ge=0, le=20)

    @model_validator(mode="after")
    def strip_and_validate_text(self):
        self.content_text = self.content_text.strip()
        if not self.content_text:
            raise ValueError("content_text는 공백일 수 없습니다.")
        return self


class MessageUpdate(BaseModel):
    content_text: str = Field(min_length=1)
    content: list[ContentPart] = Field(default_factory=list)

    @model_validator(mode="after")
    def strip_and_validate_text(self):
        self.content_text = self.content_text.strip()
        if not self.content_text:
            raise ValueError("content_text는 공백일 수 없습니다.")
        return self


class MessageRead(ORMModel):
    message_id: UUID
    session_id: UUID
    message_type: MessageType
    content: list[dict[str, Any]]
    content_text: str
    message_status: MessageStatus
    client_request_id: UUID | None
    error_code: str | None
    error_message: str | None
    metadata: dict[str, Any] = Field(validation_alias="metadata_json")
    sequence_no: int
    delete_yn: DeleteYN
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class MessageCreateResult(BaseModel):
    session_created: bool
    project_id: UUID
    session_id: UUID
    message: MessageRead
    assistant_message: MessageRead | None = None
    llm_run: LLMRunRead | None = None
    agent_run_id: UUID | None = None
    agent_run_status: str | None = None


class MessageDeleteResult(BaseModel):
    message_id: UUID
    deleted_message_count: int
    current_leaf_message_id: UUID | None
    detail: str

