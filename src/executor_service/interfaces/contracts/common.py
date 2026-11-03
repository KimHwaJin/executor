"""Common Pydantic contract primitives shared across transports."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuditFields(ContractModel):
    created_by_client: str | None
    created_by: str | None
    updated_by_client: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


class PageResponse(ContractModel):
    next_cursor: str | None
    has_more: bool


class Lifecycle(ContractModel):
    started_at: datetime | None
    finished_at: datetime | None
