"""Use-case inputs independent of transport and persistence types."""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from executor_service.domain.enums import (
    CodeSourceType,
    ExecutionMode,
    JupyterPool,
    TriggerType,
)


@dataclass(frozen=True, slots=True)
class StepSpec:
    sequence: int
    code: str | None = None
    plan_revision_id: str | None = None
    skill_name: str | None = None
    tool_name: str | None = None
    input_parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SubmitExecutionCommand:
    idempotency_key: str
    mode: ExecutionMode
    trigger_type: TriggerType
    jupyter_pool: JupyterPool
    kernel_name: str
    code_source_type: CodeSourceType
    code: str | None
    code_path: str | None
    requested_by_user_id: str
    project_id: str
    session_id: str
    execution_plan_id: str
    workflow_id: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    steps: tuple[StepSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class CancelExecutionCommand:
    execution_id: UUID
    idempotency_key: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class RetryExecutionCommand:
    execution_id: UUID
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ContinueExecutionCommand:
    execution_id: UUID
    idempotency_key: str
    expected_version: int
    step: StepSpec


@dataclass(frozen=True, slots=True)
class FinishExecutionCommand:
    execution_id: UUID
    idempotency_key: str
    expected_version: int
