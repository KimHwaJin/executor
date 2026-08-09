"""Application-facing read models for complete execution tracing."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    AttemptStatus,
    OutboxStatus,
    StepStatus,
)
from executor_service.domain.models import Execution


@dataclass(frozen=True, slots=True)
class ExecutionStepAttemptView:
    id: UUID
    execution_attempt_id: UUID
    execution_step_id: UUID
    sequence: int
    skill_name: str | None
    tool_name: str | None
    input_parameters: dict[str, Any]
    status: StepStatus
    outputs: list[dict[str, Any]]
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExecutionAttemptView:
    id: UUID
    execution_id: UUID
    attempt_number: int
    jupyter_server_id: UUID
    kernel_id: str | None
    status: AttemptStatus
    lease_owner: str
    lease_expires_at: datetime
    heartbeat_at: datetime
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None
    steps: tuple[ExecutionStepAttemptView, ...]


@dataclass(frozen=True, slots=True)
class ExecutionEventView:
    id: UUID
    event_type: str
    payload: dict[str, Any]
    delivery_status: OutboxStatus
    publish_attempt_count: int
    available_at: datetime
    created_at: datetime
    published_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class ExecutionArtifactView:
    id: UUID
    execution_id: UUID
    execution_attempt_id: UUID
    execution_step_id: UUID | None
    execution_step_attempt_id: UUID | None
    parent_artifact_id: UUID | None
    external_parent_asset_id: str | None
    artifact_type: ArtifactType
    storage_type: ArtifactStorageType
    status: ArtifactStatus
    name: str
    description: str | None
    uri: str
    relative_path: str | None
    media_type: str | None
    size_bytes: int | None
    checksum_sha256: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionTraceView:
    execution: Execution
    attempts: tuple[ExecutionAttemptView, ...]
    events: tuple[ExecutionEventView, ...]
    artifacts: tuple[ExecutionArtifactView, ...]


class ExecutionQueryService(Protocol):
    async def attempts(
        self, execution_id: UUID, *, limit: int = 100
    ) -> list[ExecutionAttemptView]: ...

    async def events(
        self, execution_id: UUID, *, limit: int = 200
    ) -> list[ExecutionEventView]: ...

    async def artifacts(
        self, execution_id: UUID, *, limit: int = 500
    ) -> list[ExecutionArtifactView]: ...

    async def artifact(self, artifact_id: UUID) -> ExecutionArtifactView: ...

    async def trace(self, execution_id: UUID) -> ExecutionTraceView: ...
