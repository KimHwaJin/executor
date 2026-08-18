"""Agent-owned models for the public Executor event and execution contracts."""

import json
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class AgentExecutionRequest(BaseModel):
    """Input accepted by the test graph for one validated SINGLE execution."""

    model_config = ConfigDict(extra="forbid")

    runtime_profile: str = "basic"
    user_id: str
    project_id: str
    session_id: str
    task_id: str
    steps: list[dict[str, Any]] = Field(min_length=1)

    def executor_payload(
        self, idempotency_key: str, *, operation_mode: str = "SINGLE"
    ) -> dict[str, Any]:
        normalized_steps = [
            {
                "sequence": sequence,
                "payload": {"type": "CODE", "content": step["code"]},
                "lineage": {
                    "skill_name": step["skill_name"],
                    "tool_name": step["tool_name"],
                    "input_parameters": step.get("input_parameters", {}),
                },
            }
            for sequence, step in enumerate(self.steps)
        ]
        return {
            "idempotency_key": idempotency_key,
            "lifecycle": {
                "operation_mode": operation_mode,
                **({"operation_wait_timeout_seconds": 600} if operation_mode == "MULTI" else {}),
            },
            "trigger": {
                "type": "INTERACTIVE",
                "actor": {"type": "AGENT", "id": "executor-test-agent"},
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
