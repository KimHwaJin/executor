from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.enums import DeleteYN
from app.schemas.schema_base import ORMModel


class UserCreate(BaseModel):
    user_name: str = Field(min_length=1, max_length=100)


class UserUpdate(BaseModel):
    user_name: str = Field(min_length=1, max_length=100)


class UserRead(ORMModel):
    user_id: UUID
    user_name: str
    delete_yn: DeleteYN
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

