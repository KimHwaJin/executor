"""Common Pydantic contract primitives shared across transports."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from executor_service.domain.enums import ActorType


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuditFields(ContractModel):
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


class ActorInput(ContractModel):
    type: ActorType
    id: str = Field(min_length=1, max_length=255)


class PageResponse(ContractModel):
    next_cursor: str | None
    has_more: bool
