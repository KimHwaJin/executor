"""Versioned contracts and factory for Executor-owned Execution events."""

import json
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from executor_service.domain.enums import (
    ActorType,
    ArtifactStatus,
    ArtifactStorageType,
    ArtifactType,
    ExecutionStatus,
    FailureType,
    RetryStrategy,
    RuntimeSessionCleanupStatus,
    StepStatus,
)
from executor_service.domain.models import OutboxEvent

EXECUTION_EVENT_SCHEMA_VERSION = "2.0"


class EventPayload(BaseModel):
    """Fields shared by every version 2 Execution event payload."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2.0"] = EXECUTION_EVENT_SCHEMA_VERSION
    execution_id: UUID


class StatusPayload(EventPayload):
    status: ExecutionStatus


class TaskPayload(StatusPayload):
    task_id: str = Field(min_length=1, max_length=255)


class StepReceiptPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sequence: int = Field(ge=0)
    step_id: UUID


class SubmittedPayload(TaskPayload):
    status: Literal[ExecutionStatus.QUEUED]
    idempotency_key: str = Field(min_length=1, max_length=255)
    operation_id: UUID
    steps: list[StepReceiptPayload]
    first_sequence: int = Field(ge=0)
    last_sequence: int = Field(ge=0)


class OperationSubmittedPayload(SubmittedPayload):
    version: int = Field(ge=0)


class FinalizationRequestedPayload(TaskPayload):
    status: Literal[ExecutionStatus.FINALIZING]
    version: int = Field(ge=0)


class WaitingForOperationPayload(StatusPayload):
    status: Literal[ExecutionStatus.WAITING_FOR_OPERATION]
    operation_id: UUID
    operation_wait_expires_at: datetime
    version: int = Field(ge=0)


class RetryRequestedPayload(TaskPayload):
    status: Literal[ExecutionStatus.QUEUED]
    operation_id: UUID
    from_sequence: int = Field(ge=0)
    retry_strategy: RetryStrategy
    previous_failure_type: FailureType | None
    retry_count: int = Field(ge=1)


class RetryDeferredPayload(StatusPayload):
    status: Literal[ExecutionStatus.QUEUED]
    failure_type: FailureType
    retry_strategy: RetryStrategy
    reason: str = Field(min_length=1, max_length=255)
    runtime_target_id: UUID


class TerminalPayload(StatusPayload):
    failure_type: FailureType | None
    retry_strategy: RetryStrategy
    retry_from_sequence: int | None = Field(default=None, ge=0)
    runtime_session_cleanup_status: RuntimeSessionCleanupStatus
    recovery_count: int | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, min_length=1, max_length=255)


class StartedPayload(StatusPayload):
    status: Literal[ExecutionStatus.RUNNING]


class RuntimeStepResult(BaseModel):
    """Transport-neutral result returned by one Runtime execution unit."""

    model_config = ConfigDict(extra="forbid")

    outputs: list[dict[str, Any]]
    execution_count: int | None = Field(default=None, ge=0)


class StepEventPayload(EventPayload):
    execution_attempt_id: UUID
    operation_id: UUID
    step_id: UUID
    sequence: int = Field(ge=0)
    status: StepStatus


class StepStartedPayload(StepEventPayload):
    status: Literal[StepStatus.RUNNING]


class StepSucceededPayload(StepEventPayload):
    status: Literal[StepStatus.SUCCEEDED]
    result: RuntimeStepResult


class StepFailedPayload(StepEventPayload):
    status: Literal[StepStatus.FAILED]
    result: RuntimeStepResult
    error_message: str = Field(min_length=1, max_length=2000)


class CancelRequestedPayload(StatusPayload):
    status: Literal[ExecutionStatus.CANCEL_REQUESTED]
    task_id: str = Field(min_length=1, max_length=255)


class SucceededPayload(TerminalPayload):
    status: Literal[ExecutionStatus.SUCCEEDED]


class FailedPayload(TerminalPayload):
    status: Literal[ExecutionStatus.FAILED]


class OperationOutcomePayload(StatusPayload):
    status: Literal[
        ExecutionStatus.WAITING_FOR_OPERATION,
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
    ]
    execution_attempt_id: UUID | None
    operation_id: UUID
    first_sequence: int = Field(ge=0)
    last_sequence: int = Field(ge=0)
    version: int = Field(ge=0)


class OperationSucceededPayload(OperationOutcomePayload):
    operation_status: Literal["SUCCEEDED"]


class OperationFailedPayload(OperationOutcomePayload):
    operation_status: Literal["FAILED"]
    failed_sequence: int | None = Field(default=None, ge=0)
    error_message: str = Field(min_length=1, max_length=2000)


class ArtifactRegisteredPayload(EventPayload):
    execution_attempt_id: UUID
    execution_step_id: UUID
    artifact_id: UUID
    artifact_type: ArtifactType
    storage_type: ArtifactStorageType
    status: ArtifactStatus
    uri: str = Field(min_length=1)


class ArtifactFailedPayload(StatusPayload):
    status: Literal[ExecutionStatus.RUNNING]
    execution_attempt_id: UUID
    sequence: int = Field(ge=0)
    error_type: str = Field(min_length=1, max_length=255)


class CleanupPayload(StatusPayload):
    status: Literal[ExecutionStatus.FAILED]
    runtime_session_cleanup_status: RuntimeSessionCleanupStatus


class CancelledPayload(StatusPayload):
    status: Literal[ExecutionStatus.CANCELLED]
    runtime_session_cleanup_status: RuntimeSessionCleanupStatus


class CleanupCompletedPayload(CleanupPayload):
    runtime_session_cleanup_status: Literal[RuntimeSessionCleanupStatus.SUCCEEDED]


class CleanupFailedPayload(CleanupPayload):
    runtime_session_cleanup_status: Literal[RuntimeSessionCleanupStatus.FAILED]


class TimeoutRequestedPayload(StatusPayload):
    status: Literal[ExecutionStatus.CANCEL_REQUESTED]
    failure_type: Literal[FailureType.EXECUTION_TIMEOUT]


class RetryWindowExpiredPayload(CleanupPayload):
    retry_was_queued: bool


ExecutionEventPayload = (
    StatusPayload
    | SubmittedPayload
    | OperationSubmittedPayload
    | FinalizationRequestedPayload
    | WaitingForOperationPayload
    | RetryRequestedPayload
    | RetryDeferredPayload
    | TerminalPayload
    | StartedPayload
    | StepStartedPayload
    | StepSucceededPayload
    | StepFailedPayload
    | CancelRequestedPayload
    | SucceededPayload
    | FailedPayload
    | OperationOutcomePayload
    | OperationSucceededPayload
    | OperationFailedPayload
    | ArtifactRegisteredPayload
    | ArtifactFailedPayload
    | CleanupPayload
    | CancelledPayload
    | CleanupCompletedPayload
    | CleanupFailedPayload
    | TimeoutRequestedPayload
    | RetryWindowExpiredPayload
)

EVENT_PAYLOAD_MODELS: dict[str, type[EventPayload]] = {
    "execution.submitted": SubmittedPayload,
    "execution.operation_submitted": OperationSubmittedPayload,
    "execution.finalization_requested": FinalizationRequestedPayload,
    "execution.waiting_for_operation": WaitingForOperationPayload,
    "execution.cancel_requested": CancelRequestedPayload,
    "execution.retry_requested": RetryRequestedPayload,
    "execution.started": StartedPayload,
    "execution.resumed": StartedPayload,
    "execution.step_started": StepStartedPayload,
    "execution.step_succeeded": StepSucceededPayload,
    "execution.step_failed": StepFailedPayload,
    "execution.retry_deferred": RetryDeferredPayload,
    "execution.operation_succeeded": OperationSucceededPayload,
    "execution.operation_failed": OperationFailedPayload,
    "execution.artifact_registered": ArtifactRegisteredPayload,
    "execution.artifact_failed": ArtifactFailedPayload,
    "execution.succeeded": SucceededPayload,
    "execution.failed": FailedPayload,
    "execution.cancelled": CancelledPayload,
    "execution.timeout_requested": TimeoutRequestedPayload,
    "execution.runtime_session_cleanup_completed": CleanupCompletedPayload,
    "execution.runtime_session_cleanup_failed": CleanupFailedPayload,
    "execution.retry_window_expired": RetryWindowExpiredPayload,
}


class ExecutionStreamEnvelope(BaseModel):
    """Redis Stream representation delivered to Agent-owned consumer groups."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: str = Field(min_length=1, max_length=255)
    schema_version: Literal["2.0"]
    aggregate_type: Literal["Execution"]
    aggregate_id: UUID
    occurred_at: datetime
    payload: dict[str, Any]
    traceparent: str | None = None
    tracestate: str | None = None

    @model_validator(mode="after")
    def validate_payload_contract(self) -> Self:
        normalized = validate_execution_event_payload(self.event_type, self.payload)
        if normalized["schema_version"] != self.schema_version:
            raise ValueError("Stream and payload schema_version values must match.")
        if normalized["execution_id"] != str(self.aggregate_id):
            raise ValueError("Stream aggregate_id must match payload execution_id.")
        self.payload = normalized
        return self

    @classmethod
    def from_redis_fields(cls, fields: dict[str, str]) -> "ExecutionStreamEnvelope":
        """Parse one decoded Redis Stream field mapping."""

        try:
            payload = json.loads(fields.get("payload", ""))
        except json.JSONDecodeError as exc:
            raise ValueError("Stream payload must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Stream payload must be a JSON object.")
        return cls.model_validate({**fields, "payload": payload})


def validate_execution_event_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and JSON-normalize one known Executor event payload."""

    model = EVENT_PAYLOAD_MODELS.get(event_type)
    if model is None:
        raise ValueError(f"Unsupported Executor event type: {event_type}")
    versioned = {"schema_version": EXECUTION_EVENT_SCHEMA_VERSION, **payload}
    return model.model_validate(versioned).model_dump(mode="json", exclude_unset=True)


def build_execution_event(
    *,
    execution_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    actor_type: ActorType | None = None,
    actor_id: str | None = None,
    traceparent: str | None = None,
    tracestate: str | None = None,
) -> OutboxEvent:
    """Build a validated version 2 event for durable Outbox persistence."""

    normalized = validate_execution_event_payload(
        event_type,
        {"execution_id": str(execution_id), **payload},
    )
    if normalized["execution_id"] != str(execution_id):
        raise ValueError("Event payload execution_id must match the aggregate execution_id.")
    return OutboxEvent(
        aggregate_type="Execution",
        aggregate_id=execution_id,
        event_type=event_type,
        payload=normalized,
        created_by_type=actor_type,
        created_by=actor_id,
        updated_by_type=actor_type,
        updated_by=actor_id,
        traceparent=traceparent,
        tracestate=tracestate,
    )
