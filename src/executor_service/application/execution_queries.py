"""Application-facing read models for execution history queries."""

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
    OperationMode,
    OperationStatus,
    OutboxStatus,
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeType,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import (
    ExecutionStep,
    NotebookProjectionStatus,
)


@dataclass(frozen=True, slots=True)
class ExecutionSummaryView:
    id: UUID
    operation_mode: OperationMode
    operation_wait_timeout_seconds: int | None
    trigger_type: TriggerType
    user_id: str
    project_id: str | None
    session_id: str | None
    task_id: str
    workflow_id: str | None
    status: ExecutionStatus
    version: int
    step_count: int
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExecutionDetailView:
    id: UUID
    operation_mode: OperationMode
    operation_wait_timeout_seconds: int | None
    trigger_type: TriggerType
    user_id: str
    project_id: str | None
    session_id: str | None
    task_id: str
    workflow_id: str | None
    runtime_type: RuntimeType
    runtime_pool: RuntimePool
    runtime_profile: str
    runtime_target_id: UUID | None
    runtime_session_id: str | None
    status: ExecutionStatus
    version: int
    cancellation_reason: str | None
    workspace_path: str | None
    notebook_path: str | None
    notebook_projection_status: NotebookProjectionStatus
    notebook_projection_attempt_count: int
    notebook_projection_error: str | None
    notebook_projected_at: datetime | None
    failure_type: FailureType | None
    error_message: str | None
    retry_strategy: RetryStrategy
    retry_count: int
    retry_from_sequence: int | None
    retained_runtime_session_until: datetime | None
    recovery_count: int
    runtime_session_cleanup_status: RuntimeSessionCleanupStatus
    runtime_abort_status: RuntimeAbortStatus
    operation_wait_expires_at: datetime | None
    execution_expires_at: datetime | None
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class ExecutionStepAttemptView:
    id: UUID
    execution_id: UUID
    execution_attempt_id: UUID
    execution_step_id: UUID
    sequence: int
    skill_name: str | None
    tool_name: str | None
    input_parameters: dict[str, Any]
    status: StepStatus
    output_summary: dict[str, Any]
    result_manifest_path: str | None
    result_manifest_checksum_sha256: str | None
    result_manifest_size_bytes: int | None
    result_fencing_token: int | None
    result_complete: bool | None
    result_representation_count: int
    result_total_size_bytes: int
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
    runtime_abort_status: RuntimeAbortStatus
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime
    finished_at: datetime | None
    step_count: int


@dataclass(frozen=True, slots=True)
class ExecutionOperationView:
    id: UUID
    execution_id: UUID
    operation_number: int
    schema_version: str
    first_sequence: int
    last_sequence: int
    operation_timeout_seconds: int | None
    metadata: dict[str, Any]
    status: OperationStatus
    execution_attempt_id: UUID | None
    error_message: str | None
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    step_count: int


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
    execution_attempt_id: UUID | None
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
class OperationResultSnapshot:
    execution: ExecutionDetailView
    operation: ExecutionOperationView
    steps: tuple[ExecutionStep, ...]


@dataclass(frozen=True, slots=True)
class ExecutionResultSnapshot:
    execution: ExecutionDetailView
    operations: tuple[ExecutionOperationView, ...]
    steps: tuple[ExecutionStep, ...]
    attempts: tuple[ExecutionAttemptView, ...]
    artifacts: tuple[ExecutionArtifactView, ...]


class ExecutionQueryService(Protocol):
    async def executions(
        self,
        *,
        user_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        workflow_id: str | None = None,
        status: ExecutionStatus | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionSummaryView]: ...

    async def execution(self, execution_id: UUID) -> ExecutionDetailView: ...

    async def steps(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionStep]: ...

    async def step(
        self, execution_id: UUID, step_id: UUID
    ) -> ExecutionStep: ...

    async def attempts(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionAttemptView]: ...

    async def attempt(
        self, execution_id: UUID, attempt_id: UUID
    ) -> ExecutionAttemptView: ...

    async def operations(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionOperationView]: ...

    async def operation(
        self, execution_id: UUID, operation_id: UUID
    ) -> ExecutionOperationView: ...

    async def operation_steps(
        self,
        execution_id: UUID,
        operation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionStep]: ...

    async def attempt_steps(
        self,
        execution_id: UUID,
        attempt_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionStepAttemptView]: ...

    async def events(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 200,
    ) -> Page[ExecutionEventView]: ...

    async def artifacts(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionArtifactView]: ...

    async def artifact(self, artifact_id: UUID) -> ExecutionArtifactView: ...

    async def operation_result_snapshot(
        self, execution_id: UUID, operation_id: UUID
    ) -> OperationResultSnapshot: ...

    async def execution_result_snapshot(
        self, execution_id: UUID
    ) -> ExecutionResultSnapshot: ...
