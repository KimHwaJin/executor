"""Framework-independent executor entities."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from executor_service.domain.enums import (
    ActorType,
    CodeSourceType,
    ExecutionStatus,
    FailureType,
    OperationMode,
    OperationStatus,
    OutboxDestination,
    OutboxStatus,
    RetryStrategy,
    RuntimeAbortStatus,
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


type NotebookProjectionStatus = Literal[
    "NOT_STARTED", "PENDING", "SUCCEEDED", "FAILED"
]


def empty_output_summary() -> dict[str, Any]:
    """Return the bounded persisted shape for a Step with no outputs."""
    return {
        "output_count": 0,
        "output_types": {},
        "stream_names": [],
        "mime_types": [],
        "has_image": False,
        "image_count": 0,
        "has_error": False,
    }


@dataclass(slots=True)
class ExecutionStep:
    sequence: int
    code: str
    source_type: CodeSourceType = CodeSourceType.INLINE
    source_path: str | None = None
    source_sha256: str = ""
    source_snapshot_path: str | None = None
    source_size_bytes: int | None = None
    step_timeout_seconds: int | None = None
    code_hash: str | None = None
    skill_name: str | None = None
    tool_name: str | None = None
    input_parameters: dict[str, Any] = field(default_factory=dict)
    operation_id: UUID | None = None
    id: UUID = field(default_factory=uuid4)
    status: StepStatus = StepStatus.PENDING
    output_summary: dict[str, Any] = field(
        default_factory=empty_output_summary
    )
    result_execution_attempt_id: UUID | None = None
    result_manifest_path: str | None = None
    result_manifest_checksum_sha256: str | None = None
    result_manifest_size_bytes: int | None = None
    result_fencing_token: int | None = None
    result_complete: bool | None = None
    result_representation_count: int = 0
    result_total_size_bytes: int = 0
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
    operation_mode: OperationMode
    trigger_type: TriggerType
    runtime_pool: RuntimePool
    runtime_profile: str
    user_id: str
    project_id: str | None
    session_id: str | None
    task_id: str
    operation_wait_timeout_seconds: int | None = None
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
    notebook_projection_status: NotebookProjectionStatus = "NOT_STARTED"
    notebook_projection_attempt_count: int = 0
    notebook_projection_error: str | None = None
    notebook_projected_at: datetime | None = None
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
    runtime_abort_status: RuntimeAbortStatus = RuntimeAbortStatus.NOT_REQUIRED
    finalization_requested: bool = False
    active_operation_id: UUID | None = None
    operation_wait_expires_at: datetime | None = None
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
        self.operation_wait_expires_at = None
        self.version += 1
        self.updated_at = utc_now()

    def request_retry(self) -> None:
        now = utc_now()
        if self.status != ExecutionStatus.FAILED:
            raise InvalidStateTransitionError(
                f"Execution {self.id} must be FAILED before retry."
            )
        if self.operation_mode != OperationMode.SINGLE:
            raise InvalidStateTransitionError(
                "Only SINGLE executions support explicit retry; MULTI Tool failures require "
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
            if (
                self.runtime_session_cleanup_status
                in {
                    RuntimeSessionCleanupStatus.PENDING,
                    RuntimeSessionCleanupStatus.FAILED,
                }
                and self.runtime_session_id is not None
            ):
                raise InvalidStateTransitionError(
                    f"Execution {self.id} has unresolved abandoned Runtime "
                    "session cleanup."
                )
            self.retry_from_sequence = 0
            self.runtime_session_id = None
            self.runtime_target_id = None
            self.retained_runtime_session_until = None
            self.runtime_session_cleanup_status = (
                RuntimeSessionCleanupStatus.NOT_REQUIRED
            )
        if self.retry_from_sequence is None:
            raise InvalidStateTransitionError(
                f"Execution {self.id} has no retry start sequence."
            )
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
            step.output_summary = empty_output_summary()
            step.result_execution_attempt_id = None
            step.error_message = None
            step.started_at = None
            step.finished_at = None
            step.updated_at = now

    def request_operation(self, expected_version: int) -> None:
        if self.operation_mode != OperationMode.MULTI:
            raise InvalidStateTransitionError(
                "Only MULTI executions accept another Operation."
            )
        if self.status != ExecutionStatus.WAITING_FOR_OPERATION:
            raise InvalidStateTransitionError(
                f"Execution {self.id} must be WAITING_FOR_OPERATION before adding an Operation."
            )
        if self.version != expected_version:
            raise ExecutionVersionConflictError(
                f"Execution version is {self.version}, expected {expected_version}."
            )
        self.status = ExecutionStatus.QUEUED
        self.finalization_requested = False
        self.operation_wait_expires_at = None
        self.updated_at = utc_now()
        self.version += 1

    def request_finalization(self, expected_version: int) -> None:
        if self.operation_mode != OperationMode.MULTI:
            raise InvalidStateTransitionError(
                "Only MULTI executions can be finalized."
            )
        if self.status != ExecutionStatus.WAITING_FOR_OPERATION:
            raise InvalidStateTransitionError(
                f"Execution {self.id} must be WAITING_FOR_OPERATION before finalization."
            )
        if self.version != expected_version:
            raise ExecutionVersionConflictError(
                f"Execution version is {self.version}, expected {expected_version}."
            )
        active_steps = [
            step
            for step in self.steps
            if step.operation_id == self.active_operation_id
        ]
        if not active_steps or any(
            step.status != StepStatus.SUCCEEDED for step in active_steps
        ):
            raise InvalidStateTransitionError(
                "Finalization requires a successful last Operation; "
                "append a corrective Operation or cancel the Execution."
            )
        self.status = ExecutionStatus.FINALIZING
        self.finalization_requested = True
        self.operation_wait_expires_at = None
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
    idempotency_key: str
    request_fingerprint: str
    schema_version: str = "1.0"
    operation_timeout_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
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
    event_sequence: int | None = None
    destination: OutboxDestination = OutboxDestination.EVENTS
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


@dataclass(slots=True)
class ExecutionEvent:
    execution_id: UUID
    event_sequence: int
    event_type: str
    payload: dict[str, Any]
    schema_version: str = "1.0"
    created_by_type: ActorType | None = None
    created_by: str | None = None
    updated_by_type: ActorType | None = None
    updated_by: str | None = None
    traceparent: str | None = None
    tracestate: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
