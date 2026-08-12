"""Application-facing read models for complete execution tracing."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from executor_service.application.pagination import Page
from executor_service.domain.enums import (
    ActorType,
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    AttemptStatus,
    ExecutionStatus,
    FailureType,
    OutboxStatus,
    RetryStrategy,
    RuntimeSessionCleanupStatus,
    RuntimeType,
    StepStatus,
)
from executor_service.domain.models import Execution, ExecutionStep


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
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExecutionAttemptView:
    id: UUID
    execution_id: UUID
    attempt_number: int
    runtime_type: RuntimeType
    runtime_profile: str
    runtime_target_id: UUID
    runtime_session_id: str | None
    status: AttemptStatus
    lease_owner: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime
    error_message: str | None
    failure_type: FailureType | None
    retry_strategy: RetryStrategy
    runtime_session_cleanup_status: RuntimeSessionCleanupStatus
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
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
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    available_at: datetime
    created_at: datetime
    updated_at: datetime
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
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionTraceView:
    execution: Execution
    attempts: Page[ExecutionAttemptView]
    events: Page[ExecutionEventView]
    artifacts: Page[ExecutionArtifactView]


class ExecutionQueryService(Protocol):
    async def executions(
        self,
        *,
        user_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        status: ExecutionStatus | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[Execution]: ...

    async def steps(
        self, execution_id: UUID, *, cursor: str | None = None, limit: int = 100
    ) -> Page[ExecutionStep]: ...

    async def attempts(
        self, execution_id: UUID, *, cursor: str | None = None, limit: int = 100
    ) -> Page[ExecutionAttemptView]: ...

    async def events(
        self, execution_id: UUID, *, cursor: str | None = None, limit: int = 200
    ) -> Page[ExecutionEventView]: ...

    async def artifacts(
        self, execution_id: UUID, *, cursor: str | None = None, limit: int = 500
    ) -> Page[ExecutionArtifactView]: ...

    async def artifact(self, artifact_id: UUID) -> ExecutionArtifactView: ...

    async def trace(self, execution_id: UUID) -> ExecutionTraceView: ...
