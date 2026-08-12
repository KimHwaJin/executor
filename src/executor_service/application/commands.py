"""Use-case inputs independent of transport and persistence types."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from executor_service.domain.enums import (
    ActorType,
    CodeSourceType,
    ExecutionMode,
    RuntimeType,
    TriggerType,
)


@dataclass(frozen=True, slots=True)
class StepSpec:
    sequence: int
    code: str
    execution_plan_id: str
    plan_step_id: str
    skill_name: str | None = None
    tool_name: str | None = None
    input_parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SubmitExecutionCommand:
    idempotency_key: str
    mode: ExecutionMode
    trigger_type: TriggerType
    runtime_profile: str
    code_source_type: CodeSourceType
    source_content: str
    code_path: str | None
    source_sha256: str
    requested_by_user_id: str
    project_id: str
    session_id: str
    task_id: str
    execution_plan_id: str
    runtime_type: RuntimeType = RuntimeType.JUPYTER
    actor_type: ActorType | None = None
    actor_id: str | None = None
    workflow_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
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
class ContinueExecutionCommand:
    execution_id: UUID
    idempotency_key: str
    expected_version: int
    step: StepSpec
    actor_type: ActorType | None = None
    actor_id: str | None = None


@dataclass(frozen=True, slots=True)
class FinishExecutionCommand:
    execution_id: UUID
    idempotency_key: str
    expected_version: int
    actor_type: ActorType | None = None
    actor_id: str | None = None
