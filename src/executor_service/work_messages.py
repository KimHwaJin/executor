"""Strict internal Redis work-message contracts.

These messages are consumed only by Executor workers. Agent and other integration
consumers use the separate versioned contracts in ``events.py``.
"""

import json
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from executor_service.domain.enums import ActorType, OutboxDestination
from executor_service.domain.models import OutboxEvent

WORK_MESSAGE_SCHEMA_VERSION = "1.0"


class WorkPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = WORK_MESSAGE_SCHEMA_VERSION
    execution_id: UUID
    operation_id: UUID | None = None


WORK_PAYLOAD_MODELS: dict[str, type[WorkPayload]] = {
    "operation.ready": WorkPayload,
    "execution.finalization_ready": WorkPayload,
    "execution.cancellation_ready": WorkPayload,
    "execution.retry_ready": WorkPayload,
}


class WorkStreamEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: UUID
    message_type: str = Field(min_length=1, max_length=255)
    schema_version: Literal["1.0"]
    aggregate_type: Literal["Execution"]
    aggregate_id: UUID
    occurred_at: datetime
    payload: dict[str, Any]
    traceparent: str | None = None
    tracestate: str | None = None

    @model_validator(mode="after")
    def validate_payload_contract(self) -> "WorkStreamEnvelope":
        normalized = validate_work_payload(self.message_type, self.payload)
        if normalized["schema_version"] != self.schema_version:
            raise ValueError(
                "Stream and payload schema_version values must match."
            )
        if normalized["execution_id"] != str(self.aggregate_id):
            raise ValueError(
                "Stream aggregate_id must match payload execution_id."
            )
        self.payload = normalized
        return self

    @classmethod
    def from_redis_fields(cls, fields: dict[str, str]) -> "WorkStreamEnvelope":
        try:
            payload = json.loads(fields.get("payload", ""))
        except json.JSONDecodeError as exc:
            raise ValueError("Stream payload must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Stream payload must be a JSON object.")
        return cls.model_validate({**fields, "payload": payload})


def validate_work_payload(
    message_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    model = WORK_PAYLOAD_MODELS.get(message_type)
    if model is None:
        raise ValueError(
            f"Unsupported Executor work message type: {message_type}"
        )
    versioned = {"schema_version": WORK_MESSAGE_SCHEMA_VERSION, **payload}
    return model.model_validate(versioned).model_dump(
        mode="json", exclude_unset=True
    )


def build_work_message(
    *,
    execution_id: UUID,
    message_type: str,
    operation_id: UUID | None = None,
    actor_type: ActorType | None = None,
    actor_id: str | None = None,
    traceparent: str | None = None,
    tracestate: str | None = None,
) -> OutboxEvent:
    normalized = validate_work_payload(
        message_type,
        {
            "execution_id": str(execution_id),
            **(
                {"operation_id": str(operation_id)}
                if operation_id is not None
                else {}
            ),
        },
    )
    return OutboxEvent(
        aggregate_type="Execution",
        aggregate_id=execution_id,
        event_type=message_type,
        payload=normalized,
        destination=OutboxDestination.WORK,
        created_by_type=actor_type,
        created_by=actor_id,
        updated_by_type=actor_type,
        updated_by=actor_id,
        traceparent=traceparent,
        tracestate=tracestate,
    )
