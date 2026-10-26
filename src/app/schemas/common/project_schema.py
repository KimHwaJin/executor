from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.core.enums import DeleteYN
from app.schemas.schema_base import ORMModel


class ProjectCreate(BaseModel):
    # 공백 문자열은 Service에서 default 요청으로 해석한 뒤 409 처리합니다.
    project_name: str = Field(max_length=200)
    system_prompt: str = ""


class ProjectUpdate(BaseModel):
    project_name: str | None = Field(default=None, min_length=1, max_length=200)
    system_prompt: str | None = None

    @model_validator(mode="after")
    def require_a_change(self):
        if self.project_name is None and self.system_prompt is None:
            raise ValueError("At least one editable field is required.")
        return self


class SessionSummary(ORMModel):
    session_id: UUID
    session_name: str
    project_id: UUID
    user_id: UUID
    message_count: int = 0
    created_at: datetime
    updated_at: datetime


class ProjectRead(ORMModel):
    project_id: UUID
    user_id: UUID
    project_name: str
    system_prompt: str
    prompt_version: int
    is_default: bool
    delete_yn: DeleteYN
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    sessions: list[SessionSummary] = Field(default_factory=list)


class ProjectDeleteResult(BaseModel):
    project_id: UUID
    project_deleted: bool
    deleted_session_count: int
    deleted_message_count: int
    detail: str

