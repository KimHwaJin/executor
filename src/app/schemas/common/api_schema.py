from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class APIModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class PageInfo(BaseModel):
    next_cursor: str | None = None
    has_next: bool


T = TypeVar("T")


class Page(APIModel, Generic[T]):
    items: list[T]
    page: PageInfo


class ProjectResource(APIModel):
    id: UUID = Field(validation_alias="project_id")
    name: str = Field(validation_alias="project_name")
    system_prompt: str
    prompt_version: int
    is_default: bool
    created_at: datetime
    updated_at: datetime


class SessionResource(APIModel):
    id: UUID = Field(validation_alias="session_id")
    project_id: UUID
    name: str = Field(validation_alias="session_name")
    current_leaf_message_id: UUID | None
    settings: dict
    created_at: datetime
    updated_at: datetime


class MessageResource(APIModel):
    id: UUID = Field(validation_alias="message_id")
    session_id: UUID
    message_type: str
    content: list[dict]
    content_text: str
    message_status: str
    # Chat UI가 일반 assistant/system/tool과 Graph agent 이름을 구분하는 표시 정보입니다.
    metadata: dict = Field(validation_alias="metadata_json")
    sequence_no: int
    created_at: datetime
    updated_at: datetime

