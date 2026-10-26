from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class JupyterServerResource(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    jupyter_server_id: UUID
    name: str
    endpoint: str
    has_token: bool
    health_status: Literal["unknown", "healthy", "unhealthy"]
    last_http_status: int | None
    last_latency_ms: int | None
    last_error: str | None
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None

