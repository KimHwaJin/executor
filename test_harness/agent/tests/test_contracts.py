"""Public integration contract tests with no Executor implementation imports."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from executor_test_agent.integrations.contracts import (
    AgentExecutionRequest,
    ExecutionEventBatch,
    ExecutionEventEnvelope,
)


def test_execution_request_builds_single_executor_spec() -> None:
    request = AgentExecutionRequest(
        user_id="user",
        project_id="project",
        session_id="session",
        task_id="task",
        steps=[{"skill_name": "eda", "tool_name": "sum", "code": "print(3)"}],
    )

    payload = request.executor_payload("idempotency")

    assert payload["operation"]["spec"]["schema_version"] == "1.0"
    assert payload["operation"]["spec"]["steps"][0]["sequence"] == 0
    assert payload["context"]["task_id"] == "task"
    assert payload["lifecycle"] == {"operation_mode": "SINGLE"}


def test_execution_request_builds_multi_executor_spec() -> None:
    request = AgentExecutionRequest(
        user_id="user",
        project_id="project",
        session_id="session",
        task_id="task",
        operation_mode="MULTI",
        operation_wait_timeout_seconds=900,
        steps=[{"skill_name": "eda", "tool_name": "sum", "code": "value = 3"}],
        follow_up_operations=[
            [{"skill_name": "report", "tool_name": "show", "code": "print(value)"}]
        ],
    )

    payload = request.executor_payload("idempotency")

    assert payload["lifecycle"] == {
        "operation_mode": "MULTI",
        "operation_wait_timeout_seconds": 900,
    }


def test_single_execution_rejects_follow_up_operations() -> None:
    with pytest.raises(ValidationError, match="require MULTI"):
        AgentExecutionRequest(
            user_id="user",
            project_id="project",
            session_id="session",
            task_id="task",
            steps=[{"skill_name": "eda", "tool_name": "sum", "code": "value = 3"}],
            follow_up_operations=[
                [{"skill_name": "report", "tool_name": "show", "code": "print(value)"}]
            ],
        )


def test_event_envelope_validates_v1_fields() -> None:
    execution_id = uuid4()
    event = ExecutionEventEnvelope.from_redis_fields(
        {
            "event_id": str(uuid4()),
            "event_type": "execution.completed",
            "schema_version": "1.0",
            "execution_id": str(execution_id),
            "occurred_at": "2026-08-13T00:00:00Z",
            "payload": json.dumps(
                {
                    "status": "SUCCEEDED",
                }
            ),
        }
    )

    assert event.execution_id == execution_id
    assert event.payload["status"] == "SUCCEEDED"


def test_event_batch_requires_wake_event_to_be_last() -> None:
    execution_id = uuid4()
    first = ExecutionEventEnvelope.model_validate(
        {
            "event_id": uuid4(),
            "event_type": "execution.step_completed",
            "schema_version": "1.0",
            "execution_id": execution_id,
            "occurred_at": "2026-08-13T00:00:00Z",
            "payload": {
                "status": "SUCCEEDED",
            },
        }
    )
    wake = first.model_copy(
        update={"event_id": uuid4(), "event_type": "execution.operation_completed"}
    )

    batch = ExecutionEventBatch(events=[first, wake], wake_event=wake)
    assert batch.wake_event.event_type == "execution.operation_completed"

    with pytest.raises(ValidationError, match="last event"):
        ExecutionEventBatch(events=[first, wake], wake_event=first)

    with pytest.raises(ValidationError, match="duplicate event_id"):
        ExecutionEventBatch(events=[first, first], wake_event=first)


def test_event_envelope_rejects_legacy_envelope_fields() -> None:
    with pytest.raises(ValidationError):
        ExecutionEventEnvelope.from_redis_fields(
            {
                "event_id": str(uuid4()),
                "event_type": "execution.completed",
                "schema_version": "1.0",
                "aggregate_type": "Execution",
                "aggregate_id": str(uuid4()),
                "execution_id": str(uuid4()),
                "occurred_at": "2026-08-13T00:00:00Z",
                "payload": json.dumps(
                    {
                        "status": "FAILED",
                    }
                ),
            }
        )
