from datetime import datetime
from uuid import UUID

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import TaskStatus


class TaskResource(BaseModel):
    """Session lock 관찰용 응답. lock_token은 보안상 API에 노출하지 않습니다."""

    model_config = ConfigDict(from_attributes=True)

    task_id: UUID
    graph_task_id: UUID | None
    root_run_id: UUID | None
    checkpoint_run_id: UUID | None
    session_id: UUID
    trigger_message_id: UUID | None
    trigger_type: str
    status: TaskStatus
    is_active: bool
    lock_owner: str | None
    heartbeat_at: datetime | None
    lease_expires_at: datetime | None
    cancel_requested_at: datetime | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class TaskResume(BaseModel):
    """waiting_input Task를 같은 장기 Job 안에서 재개하는 입력입니다."""

    command: dict[str, Any] | str
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskCancel(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)
