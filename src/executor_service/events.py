"""Public Redis contracts and factory for Executor Execution events."""

import json
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from executor_service.domain.enums import ActorType
from executor_service.domain.models import OutboxEvent

EXECUTION_EVENT_SCHEMA_VERSION = "1.0"

type ExecutionEventType = Literal[
    "execution.started",
    "execution.operation_started",
    "execution.step_started",
    "execution.step_completed",
    "execution.operation_completed",
    "execution.completed",
]


class ContractModel(BaseModel):
    """Strict base for the public event contract."""

    model_config = ConfigDict(extra="forbid")


class RuntimeSummary(ContractModel):
    provider: str = Field(min_length=1, max_length=64)
    profile: str = Field(min_length=1, max_length=128)
    target_id: UUID
    session_id: str | None = Field(default=None, max_length=255)


class OperationReference(ContractModel):
    id: UUID
    number: int = Field(ge=1)


class StartedOperationReference(OperationReference):
    step_count: int = Field(ge=1)


class StepReference(ContractModel):
    id: UUID
    sequence: int = Field(ge=0)


class AttemptReference(ContractModel):
    id: UUID
    number: int = Field(ge=1)
    reason: Literal["INITIAL", "RETRY"]


class ResultReference(ContractModel):
    storage: Literal["SHARED_PV"]
    relative_path: str = Field(min_length=1, max_length=4096)
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_relative_path(self) -> Self:
        path = self.relative_path.replace("\\", "/")
        if path.startswith("/") or any(
            part in {"", ".", ".."} for part in path.split("/")
        ):
            raise ValueError("Result relative_path must stay below its root.")
        self.relative_path = path
        return self


class OutputSummary(ContractModel):
    count: int = Field(ge=0)
    content_types: list[str]

    @model_validator(mode="after")
    def validate_content_types(self) -> Self:
        normalized = sorted(set(self.content_types))
        if any(not item.strip() for item in normalized):
            raise ValueError("Output content types must not be blank.")
        self.content_types = normalized
        return self


class ErrorSummary(ContractModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool


class OperationErrorSummary(ErrorSummary):
    step_id: UUID | None = None


class ExecutionErrorSummary(OperationErrorSummary):
    operation_id: UUID | None = None


class StepSummary(ContractModel):
    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        terminal = self.succeeded + self.failed + self.cancelled
        if self.completed != terminal or self.completed > self.total:
            raise ValueError("Step summary counts are inconsistent.")
        return self


class OperationSummary(ContractModel):
    total: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.succeeded + self.failed + self.cancelled > self.total:
            raise ValueError("Operation summary counts are inconsistent.")
        return self


class Continuation(ContractModel):
    allowed: Literal[True]
    expected_version: int = Field(ge=0)
    expires_at: datetime


class RetryAvailability(ContractModel):
    allowed: Literal[True]
    from_step_id: UUID
    expires_at: datetime


class ExecutionStartedPayload(ContractModel):
    status: Literal["RUNNING"]
    runtime: RuntimeSummary


class OperationStartedPayload(ContractModel):
    status: Literal["RUNNING"]
    operation: StartedOperationReference


class StepStartedPayload(ContractModel):
    status: Literal["RUNNING"]
    operation: OperationReference
    step: StepReference
    attempt: AttemptReference


class StepCompletedPayload(ContractModel):
    status: Literal["SUCCEEDED", "FAILED", "CANCELLED"]
    operation: OperationReference
    step: StepReference
    attempt: AttemptReference
    result_ref: ResultReference | None
    output_summary: OutputSummary | None
    error: ErrorSummary | None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status == "SUCCEEDED":
            if self.result_ref is None or self.output_summary is None:
                raise ValueError(
                    "A successful Step requires its persisted result."
                )
            if self.error is not None:
                raise ValueError("A successful Step cannot contain an error.")
        elif self.error is None:
            raise ValueError("A failed or cancelled Step requires an error.")
        return self


class StepResult(ContractModel):
    step_id: UUID
    sequence: int = Field(ge=0)
    status: Literal["SUCCEEDED", "FAILED", "CANCELLED"]
    attempt: AttemptReference
    result_ref: ResultReference | None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status == "SUCCEEDED" and self.result_ref is None:
            raise ValueError("A successful Step result requires a reference.")
        return self


class OperationCompletedPayload(ContractModel):
    status: Literal["SUCCEEDED", "FAILED", "CANCELLED"]
    execution_status: Literal[
        "WAITING_FOR_OPERATION", "SUCCEEDED", "FAILED", "CANCELLED"
    ]
    operation: OperationReference
    step_summary: StepSummary
    step_results: list[StepResult]
    continuation: Continuation | None
    error: OperationErrorSummary | None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status == "SUCCEEDED" and self.error is not None:
            raise ValueError("A successful Operation cannot contain an error.")
        if self.status != "SUCCEEDED" and self.error is None:
            raise ValueError(
                "A failed or cancelled Operation requires an error."
            )
        if self.continuation is not None and self.execution_status != (
            "WAITING_FOR_OPERATION"
        ):
            raise ValueError("Continuation requires WAITING_FOR_OPERATION.")
        sequences = [item.sequence for item in self.step_results]
        if sequences != sorted(set(sequences)):
            raise ValueError(
                "Operation Step results must have unique ordered sequences."
            )
        return self


class ExecutionCompletedPayload(ContractModel):
    status: Literal["SUCCEEDED", "FAILED", "CANCELLED"]
    operation_summary: OperationSummary
    retry: RetryAvailability | None
    error: ExecutionErrorSummary | None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status == "SUCCEEDED":
            if self.retry is not None or self.error is not None:
                raise ValueError(
                    "A successful Execution cannot contain retry or error."
                )
        elif self.error is None:
            raise ValueError(
                "A failed or cancelled Execution requires an error."
            )
        if self.status != "FAILED" and self.retry is not None:
            raise ValueError("Only a failed Execution can be retried.")
        return self


type ExecutionEventPayload = (
    ExecutionStartedPayload
    | OperationStartedPayload
    | StepStartedPayload
    | StepCompletedPayload
    | OperationCompletedPayload
    | ExecutionCompletedPayload
)

EVENT_PAYLOAD_MODELS: dict[str, type[ContractModel]] = {
    "execution.started": ExecutionStartedPayload,
    "execution.operation_started": OperationStartedPayload,
    "execution.step_started": StepStartedPayload,
    "execution.step_completed": StepCompletedPayload,
    "execution.operation_completed": OperationCompletedPayload,
    "execution.completed": ExecutionCompletedPayload,
}


class ExecutionStreamEnvelope(ContractModel):
    """Ordered Redis representation delivered to external consumers."""

    event_id: UUID
    event_type: ExecutionEventType
    schema_version: Literal["1.0"]
    execution_id: UUID
    event_sequence: int = Field(ge=1)
    payload: dict[str, Any]
    occurred_at: datetime

    @model_validator(mode="after")
    def validate_payload_contract(self) -> Self:
        self.payload = validate_execution_event_payload(
            self.event_type, self.payload
        )
        return self

    @classmethod
    def from_redis_fields(
        cls, fields: dict[str, str]
    ) -> "ExecutionStreamEnvelope":
        """Parse one decoded Redis Stream field mapping."""

        try:
            payload = json.loads(fields.get("payload", ""))
        except json.JSONDecodeError as exc:
            raise ValueError("Stream payload must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Stream payload must be a JSON object.")
        return cls.model_validate({**fields, "payload": payload})


def validate_execution_event_payload(
    event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Validate and JSON-normalize one public Executor event payload."""

    model = EVENT_PAYLOAD_MODELS.get(event_type)
    if model is None:
        raise ValueError(f"Unsupported Executor event type: {event_type}")
    return model.model_validate(payload).model_dump(
        mode="json", exclude_unset=True
    )


def build_execution_event(
    *,
    execution_id: UUID,
    event_sequence: int,
    event_type: str,
    payload: dict[str, Any],
    actor_type: ActorType | None = None,
    actor_id: str | None = None,
    traceparent: str | None = None,
    tracestate: str | None = None,
) -> OutboxEvent:
    """Build one validated public event for durable Outbox persistence."""

    normalized = validate_execution_event_payload(event_type, payload)
    return OutboxEvent(
        aggregate_type="Execution",
        aggregate_id=execution_id,
        event_type=event_type,
        payload=normalized,
        event_sequence=event_sequence,
        created_by_type=actor_type,
        created_by=actor_id,
        updated_by_type=actor_type,
        updated_by=actor_id,
        traceparent=traceparent,
        tracestate=tracestate,
    )
