from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.enums import DeleteYN
from app.schemas.schema_base import ORMModel
from app.schemas.common.message_schema import MessageRead


class SessionCreate(BaseModel):
    session_name: str | None = Field(default=None, max_length=300)
    settings: dict[str, Any] = Field(default_factory=dict)


class SessionUpdate(BaseModel):
    session_name: str | None = Field(default=None, min_length=1, max_length=300)

    # 현재 project_id와 구분하기 위해 이동 대상은 별도 이름을 사용합니다.
    target_project_id: UUID | None = None


class SessionRead(ORMModel):
    session_id: UUID
    session_name: str
    project_id: UUID
    user_id: UUID
    current_leaf_message_id: UUID | None
    settings: dict[str, Any]
    delete_yn: DeleteYN
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    messages: list[MessageRead] = Field(default_factory=list)


class SessionDeleteResult(BaseModel):
    session_id: UUID
    deleted_message_count: int
    detail: str

