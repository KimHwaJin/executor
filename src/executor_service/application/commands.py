"""Use-case inputs independent of transport and persistence types."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from executor_service.domain.enums import (
    ActorType,
    ArtifactType,
    CodeSourceType,
    OperationMode,
    RuntimeType,
    TriggerType,
)


@dataclass(frozen=True, slots=True)
class StepSpec:
    sequence: int
    code: str
    source_type: CodeSourceType = CodeSourceType.INLINE
    source_path: str | None = None
    source_sha256: str = ""
    step_timeout_seconds: int | None = None
    skill_name: str | None = None
    tool_name: str | None = None
    input_parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SubmitExecutionCommand:
    idempotency_key: str
    operation_mode: OperationMode
    trigger_type: TriggerType
    runtime_profile: str
    user_id: str
    project_id: str | None
    session_id: str | None
    task_id: str
    spec_schema_version: str = "1.0"
    operation_wait_timeout_seconds: int | None = None
    operation_timeout_seconds: int | None = None
    runtime_type: RuntimeType = RuntimeType.JUPYTER
    actor_type: ActorType | None = None
    actor_id: str | None = None
    workflow_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    operation_metadata: dict[str, Any] = field(default_factory=dict)
    steps: tuple[StepSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class CancelExecutionCommand:
    execution_id: UUID
    idempotency_key: str
    reason: str | None = None
    actor_type: ActorType | None = None
    actor_id: str | None = None


@dataclass(frozen=True, slots=True)
class RetryExecutionCommand:
    execution_id: UUID
    idempotency_key: str
    actor_type: ActorType | None = None
    actor_id: str | None = None


@dataclass(frozen=True, slots=True)
class CreateOperationCommand:
    execution_id: UUID
    idempotency_key: str
    expected_version: int
    steps: tuple[StepSpec, ...]
    spec_schema_version: str = "1.0"
    operation_timeout_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    actor_type: ActorType | None = None
    actor_id: str | None = None


@dataclass(frozen=True, slots=True)
class FinalizeExecutionCommand:
    execution_id: UUID
    idempotency_key: str
    expected_version: int
    actor_type: ActorType | None = None
    actor_id: str | None = None


@dataclass(frozen=True, slots=True)
class MaterializeArtifactCommand:
    execution_id: UUID
    idempotency_key: str
    artifact_type: ArtifactType
    source_type: CodeSourceType
    source_content: str | None
    source_path: str | None
    source_sha256: str | None
    name: str | None = None
    description: str | None = None
    media_type: str | None = None
    append_to_notebook: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    actor_type: ActorType | None = None
    actor_id: str | None = None
