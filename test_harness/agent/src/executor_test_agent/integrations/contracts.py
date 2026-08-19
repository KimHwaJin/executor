"""Agent-owned models for the public Executor event and execution contracts."""

import json
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from executor_test_agent.code_policy import PlannedStep


class ExecutionEventEnvelope(BaseModel):
    """Version 2 Redis envelope without importing Executor implementation types."""

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: str = Field(pattern=r"^execution\.")
    schema_version: Literal["2.0"]
    aggregate_type: Literal["Execution"]
    aggregate_id: UUID
    occurred_at: datetime
    payload: dict[str, Any]
    traceparent: str | None = None
    tracestate: str | None = None

    @model_validator(mode="after")
    def validate_common_payload(self) -> Self:
        if self.payload.get("schema_version") != self.schema_version:
            raise ValueError("Stream and payload schema versions must match.")
        if self.payload.get("execution_id") != str(self.aggregate_id):
            raise ValueError("Stream aggregate_id must match payload execution_id.")
        return self

    @classmethod
    def from_redis_fields(cls, fields: dict[str, str]) -> "ExecutionEventEnvelope":
        try:
            payload = json.loads(fields.get("payload", ""))
        except json.JSONDecodeError as exc:
            raise ValueError("Executor event payload must be valid JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Executor event payload must be a JSON object.")
        return cls.model_validate({**fields, "payload": payload})


class ExecutionEventBatch(BaseModel):
    """Events observed for one Execution up to and including a wake-up event."""

    model_config = ConfigDict(extra="forbid")

    events: list[ExecutionEventEnvelope] = Field(min_length=1)
    wake_event: ExecutionEventEnvelope

    @model_validator(mode="after")
    def validate_wake_event(self) -> Self:
        if self.events[-1].event_id != self.wake_event.event_id:
            raise ValueError("wake_event must be the last event in the batch.")
        execution_ids = {event.aggregate_id for event in self.events}
        if execution_ids != {self.wake_event.aggregate_id}:
            raise ValueError("Every event in a batch must belong to the same Execution.")
        event_ids = [event.event_id for event in self.events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("An event batch cannot contain duplicate event_id values.")
        return self


class AgentExecutionRequest(BaseModel):
    """Deterministic SINGLE or MULTI scenario accepted by the test graph."""

    model_config = ConfigDict(extra="forbid")

    runtime_profile: str = "basic"
    actor_id: str = Field(default="executor-test-agent", min_length=1, max_length=255)
    user_id: str
    project_id: str
    session_id: str
    task_id: str
    operation_mode: Literal["SINGLE", "MULTI"] = "SINGLE"
    operation_wait_timeout_seconds: int = Field(default=600, ge=30)
    steps: list[PlannedStep] = Field(min_length=1)
    follow_up_operations: list[list[PlannedStep]] = Field(default_factory=list)
    auto_finalize: bool = True

    @model_validator(mode="after")
    def validate_scenario(self) -> Self:
        if self.operation_mode == "SINGLE" and self.follow_up_operations:
            raise ValueError("follow_up_operations require MULTI operation_mode.")
        if any(not operation for operation in self.follow_up_operations):
            raise ValueError("Every follow-up Operation requires at least one Step.")
        return self

    def executor_payload(self, idempotency_key: str) -> dict[str, Any]:
        normalized_steps = [
            {
                "sequence": sequence,
                "payload": {"type": "CODE", "content": step.code},
                "lineage": {
                    "skill_name": step.skill_name,
                    "tool_name": step.tool_name,
                    "input_parameters": {},
                },
            }
            for sequence, step in enumerate(self.steps)
        ]
        return {
            "idempotency_key": idempotency_key,
            "lifecycle": {
                "operation_mode": self.operation_mode,
                **(
                    {
                        "operation_wait_timeout_seconds": self.operation_wait_timeout_seconds,
                    }
                    if self.operation_mode == "MULTI"
                    else {}
                ),
            },
            "trigger": {
                "type": "INTERACTIVE",
                "actor": {"type": "AGENT", "id": self.actor_id},
            },
            "runtime": {"type": "JUPYTER", "profile": self.runtime_profile},
            "operation": {
                "source": {
                    "type": "INLINE",
                    "spec": {"schema_version": "1.0", "steps": normalized_steps},
                },
            },
            "context": {
                "user_id": self.user_id,
                "project_id": self.project_id,
                "session_id": self.session_id,
                "task_id": self.task_id,
            },
        }
