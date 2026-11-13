"""Common Pydantic contract primitives shared across transports."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from executor_service.application.execution_queries import StatusReason
from executor_service.domain.enums import (
    FailureType,
    FinalizedBy,
    StatusReasonCode,
)


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


class StatusReasonResponse(ContractModel):
    """Why the resource is in its status."""

    code: FailureType | FinalizedBy | StatusReasonCode = Field(
        description=(
            "A FailureType for FAILED; AGENT, WAIT_TIMEOUT or MAX_RUNTIME "
            "for an Execution ended by finalization or its maximum runtime; "
            "otherwise a status-specific code such as CAPACITY_FULL for "
            "QUEUED."
        ),
    )
    message: str
    detail: dict[str, Any] | None = Field(
        description=(
            "Values behind the code: queue_position and per-Target targets "
            "for QUEUED, handover_reason and paused_since for "
            "WORKER_HANDOVER. Null when the code needs none."
        ),
    )

    @classmethod
    def from_view(
        cls, reason: StatusReason | None
    ) -> "StatusReasonResponse | None":
        if reason is None:
            return None
        return cls(
            code=reason.code, message=reason.message, detail=reason.detail
        )
