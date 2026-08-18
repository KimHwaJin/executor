"""Public integration contract tests with no Executor implementation imports."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from executor_test_agent.integrations.contracts import (
    AgentExecutionRequest,
    ExecutionEventEnvelope,
)


def test_execution_request_builds_executor_spec_v1() -> None:
    request = AgentExecutionRequest(
        user_id="user",
        project_id="project",
        session_id="session",
        task_id="task",
        steps=[{"skill_name": "eda", "tool_name": "sum", "code": "print(3)"}],
    )

    payload = request.executor_payload("idempotency")

    assert payload["operation"]["source"]["spec"]["schema_version"] == "1.0"
    assert payload["operation"]["source"]["spec"]["steps"][0]["sequence"] == 0
    assert payload["context"]["task_id"] == "task"


def test_event_envelope_validates_common_v1_fields() -> None:
    execution_id = uuid4()
    event = ExecutionEventEnvelope.from_redis_fields(
        {
            "event_id": str(uuid4()),
            "event_type": "execution.succeeded",
            "schema_version": "2.0",
            "aggregate_type": "Execution",
            "aggregate_id": str(execution_id),
            "occurred_at": "2026-08-13T00:00:00Z",
            "payload": json.dumps(
                {
                    "schema_version": "2.0",
                    "execution_id": str(execution_id),
                    "status": "SUCCEEDED",
                }
            ),
        }
    )

    assert event.aggregate_id == execution_id
    assert event.payload["status"] == "SUCCEEDED"


def test_event_envelope_rejects_mismatched_execution_id() -> None:
    with pytest.raises(ValidationError):
        ExecutionEventEnvelope.from_redis_fields(
            {
                "event_id": str(uuid4()),
                "event_type": "execution.failed",
                "schema_version": "2.0",
                "aggregate_type": "Execution",
                "aggregate_id": str(uuid4()),
                "occurred_at": "2026-08-13T00:00:00Z",
                "payload": json.dumps(
                    {
                        "schema_version": "2.0",
                        "execution_id": str(uuid4()),
                        "status": "FAILED",
                    }
                ),
            }
        )
