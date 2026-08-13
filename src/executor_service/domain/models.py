"""Framework-independent executor entities."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from executor_service.domain.enums import (
    ActorType,
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    FailureType,
    OperationStatus,
    OutboxStatus,
    RetryStrategy,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    RuntimeType,
    StepStatus,
    TriggerType,
)
from executor_service.domain.errors import (
    ExecutionVersionConflictError,
    InvalidStateTransitionError,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class ExecutionStep:
    sequence: int
    code: str
    execution_plan_id: str
    plan_step_id: str
    code_hash: str | None = None
    skill_name: str | None = None
    tool_name: str | None = None
    input_parameters: dict[str, Any] = field(default_factory=dict)
    operation_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)
    status: StepStatus = StepStatus.PENDING
    outputs: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None
    created_by_type: ActorType | None = None
    created_by: str | None = None
    updated_by_type: ActorType | None = None
    updated_by: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(slots=True)
class Execution:
    idempotency_key: str
    request_fingerprint: str
    mode: ExecutionMode
    trigger_type: TriggerType
    runtime_pool: RuntimePool
    runtime_profile: str
    code_source_type: CodeSourceType
    source_content: str
    code_path: str | None
    source_sha256: str
    user_id: str
    project_id: str
    session_id: str
    task_id: str
    execution_plan_id: str
    runtime_type: RuntimeType = RuntimeType.JUPYTER
    workflow_id: str | None = None
    created_by_type: ActorType | None = None
    created_by: str | None = None
    updated_by_type: ActorType | None = None
    updated_by: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    steps: list[ExecutionStep] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)
    status: ExecutionStatus = ExecutionStatus.QUEUED
    cancel_idempotency_key: str | None = None
    cancellation_reason: str | None = None
    runtime_target_id: UUID | None = None
    runtime_session_id: str | None = None
    workspace_path: str | None = None
    notebook_path: str | None = None
    error_message: str | None = None
    failure_type: FailureType | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    retry_strategy: RetryStrategy = RetryStrategy.NOT_RETRYABLE
    retry_from_sequence: int | None = None
    retained_runtime_session_until: datetime | None = None
    retry_count: int = 0
    recovery_count: int = 0
    runtime_session_cleanup_status: RuntimeSessionCleanupStatus = (
        RuntimeSessionCleanupStatus.NOT_REQUIRED
    )
    dynamic_finish_requested: bool = False
    active_operation_id: UUID | None = None
    dynamic_wait_expires_at: datetime | None = None
    execution_expires_at: datetime | None = None
    traceparent: str | None = None
    tracestate: str | None = None
    version: int = 0
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def request_cancel(self, idempotency_key: str, reason: str | None) -> None:
        if self.status.is_terminal:
            raise InvalidStateTransitionError(
                f"Execution {self.id} is already terminal ({self.status})."
            )
        if self.status == ExecutionStatus.CANCEL_REQUESTED:
            return
        self.status = ExecutionStatus.CANCEL_REQUESTED
        self.cancel_idempotency_key = idempotency_key
        self.cancellation_reason = reason
        self.dynamic_wait_expires_at = None
        self.version += 1
        self.updated_at = utc_now()

    def request_retry(self) -> None:
        now = utc_now()
        if self.status != ExecutionStatus.FAILED:
            raise InvalidStateTransitionError(f"Execution {self.id} must be FAILED before retry.")
        if self.mode != ExecutionMode.STATIC:
            raise InvalidStateTransitionError(
                "Only STATIC executions support explicit retry; DYNAMIC Tool failures require "
                "a correction Operation."
            )
        if self.active_operation_id is None:
            raise InvalidStateTransitionError(
                f"Execution {self.id} has no active Operation to retry."
            )
        if self.retry_strategy == RetryStrategy.NOT_RETRYABLE:
            raise InvalidStateTransitionError(
                f"Execution {self.id} has no supported retry strategy."
            )
        if self.retry_strategy == RetryStrategy.FROM_FAILED_STEP and (
            self.retry_from_sequence is None
            or self.runtime_session_id is None
            or self.runtime_target_id is None
            or self.retained_runtime_session_until is None
            or _with_utc(self.retained_runtime_session_until) <= now
        ):
            raise InvalidStateTransitionError(
                f"Execution {self.id} has no resumable retained Runtime session."
            )
        if self.retry_strategy == RetryStrategy.FROM_START:
            if self.runtime_session_cleanup_status == RuntimeSessionCleanupStatus.PENDING:
                raise InvalidStateTransitionError(
                    f"Execution {self.id} is still cleaning up its abandoned Runtime session."
                )
            self.retry_from_sequence = 0
            self.runtime_session_id = None
            self.runtime_target_id = None
            self.retained_runtime_session_until = None
            self.runtime_session_cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
        if self.retry_from_sequence is None:
            raise InvalidStateTransitionError(f"Execution {self.id} has no retry start sequence.")
        self.status = ExecutionStatus.QUEUED
        self.error_message = None
        self.finished_at = None
        self.lease_owner = None
        self.lease_expires_at = None
        self.heartbeat_at = None
        self.retry_count += 1
        self.version += 1
        self.updated_at = now
        for step in self.steps:
            if step.sequence < self.retry_from_sequence:
                continue
            step.status = StepStatus.PENDING
            step.outputs = []
            step.error_message = None
            step.started_at = None
            step.finished_at = None
            step.updated_at = now

    def request_dynamic_continue(self, expected_version: int) -> None:
        if self.mode != ExecutionMode.DYNAMIC:
            raise InvalidStateTransitionError("Only DYNAMIC executions can be continued.")
        if self.status != ExecutionStatus.WAITING_FOR_CONTINUE:
            raise InvalidStateTransitionError(
                f"Execution {self.id} must be WAITING_FOR_CONTINUE before continue."
            )
        if self.version != expected_version:
            raise ExecutionVersionConflictError(
                f"Execution version is {self.version}, expected {expected_version}."
            )
        self.status = ExecutionStatus.QUEUED
        self.dynamic_finish_requested = False
        self.dynamic_wait_expires_at = None
        self.updated_at = utc_now()
        self.version += 1

    def request_dynamic_finish(self, expected_version: int) -> None:
        if self.mode != ExecutionMode.DYNAMIC:
            raise InvalidStateTransitionError("Only DYNAMIC executions can be finished.")
        if self.status != ExecutionStatus.WAITING_FOR_CONTINUE:
            raise InvalidStateTransitionError(
                f"Execution {self.id} must be WAITING_FOR_CONTINUE before finish."
            )
        if self.version != expected_version:
            raise ExecutionVersionConflictError(
                f"Execution version is {self.version}, expected {expected_version}."
            )
        self.status = ExecutionStatus.QUEUED
        self.dynamic_finish_requested = True
        self.dynamic_wait_expires_at = None
        self.updated_at = utc_now()
        self.version += 1


def _with_utc(value: datetime) -> datetime:
    """SQLite test adapters may return timezone-naive values for aware columns."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(slots=True)
class ExecutionOperation:
    execution_id: UUID
    operation_number: int
    first_sequence: int
    last_sequence: int
    execution_plan_id: str
    code_source_type: CodeSourceType
    code_path: str | None
    source_sha256: str
    idempotency_key: str
    request_fingerprint: str
    status: OperationStatus = OperationStatus.QUEUED
    execution_attempt_id: UUID | None = None
    error_message: str | None = None
    created_by_type: ActorType | None = None
    created_by: str | None = None
    updated_by_type: ActorType | None = None
    updated_by: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(slots=True)
class OutboxEvent:
    aggregate_type: str
    aggregate_id: UUID
    event_type: str
    payload: dict[str, Any]
    created_by_type: ActorType | None = None
    created_by: str | None = None
    updated_by_type: ActorType | None = None
    updated_by: str | None = None
    traceparent: str | None = None
    tracestate: str | None = None
    id: UUID = field(default_factory=uuid4)
    status: OutboxStatus = OutboxStatus.PENDING
    attempt_count: int = 0
    available_at: datetime = field(default_factory=utc_now)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    published_at: datetime | None = None
    last_error: str | None = None
