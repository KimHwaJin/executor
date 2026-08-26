import json
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import OperationMode, TriggerType
from executor_service.events import (
    EVENT_PAYLOAD_MODELS,
    EXECUTION_EVENT_SCHEMA_VERSION,
    ExecutionStreamEnvelope,
    build_execution_event,
)
from executor_service.infrastructure.db.models import OutboxEventORM
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.outbox import OutboxPublisher
from executor_service.tracing import TracingManager


class RecordingRedis:
    def __init__(self) -> None:
        self.messages: dict[str, list[dict[str, Any]]] = {}

    async def xadd(self, stream: str, fields: dict[FieldT, EncodableT]) -> str:
        self.messages.setdefault(stream, []).append(
            {str(key): value for key, value in fields.items()}
        )
        return "1-0"


def _submit_command() -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key="event-contract-v1",
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="event-user",
        project_id="event-project",
        session_id="event-session",
        task_id="event-task",
        steps=(StepSpec(sequence=0, code="print('event contract')"),),
    )


def _started_payload() -> dict[str, object]:
    return {
        "status": "RUNNING",
        "runtime": {
            "provider": "JUPYTER",
            "profile": "basic",
            "target_id": str(uuid4()),
            "session_id": "kernel-1",
        },
    }


def test_public_event_set_is_small_and_versioned_at_envelope() -> None:
    assert set(EVENT_PAYLOAD_MODELS) == {
        "execution.started",
        "execution.operation_started",
        "execution.step_started",
        "execution.step_completed",
        "execution.operation_completed",
        "execution.completed",
    }
    assert EXECUTION_EVENT_SCHEMA_VERSION == "1.0"
    assert all(
        "schema_version" not in model.model_fields
        for model in EVENT_PAYLOAD_MODELS.values()
    )


def test_event_factory_rejects_unknown_missing_and_extra_fields() -> None:
    execution_id = uuid4()
    with pytest.raises(ValueError, match="Unsupported Executor event type"):
        build_execution_event(
            execution_id=execution_id,
            event_type="execution.unknown",
            payload={"status": "RUNNING"},
        )
    with pytest.raises(ValidationError):
        build_execution_event(
            execution_id=execution_id,
            event_type="execution.started",
            payload={"status": "RUNNING"},
        )
    with pytest.raises(ValidationError):
        build_execution_event(
            execution_id=execution_id,
            event_type="execution.started",
            payload={**_started_payload(), "unexpected": True},
        )


async def test_event_stream_serializes_only_the_six_public_fields(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(_submit_command())
    event = build_execution_event(
        execution_id=execution.id,
        event_type="execution.started",
        payload=_started_payload(),
    )
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(OutboxEventORM.from_domain(event))

    recording_redis = RecordingRedis()
    publisher = OutboxPublisher(
        session_factory=session_factory,
        redis=cast(Redis, recording_redis),
        work_stream_name="work-contract-v1",
        event_stream_name="event-contract-v1",
        poll_interval_seconds=0.01,
        batch_size=10,
        tracing=TracingManager(Settings(runtime_enabled=False)),
    )

    assert await publisher.publish_batch() == 2
    raw_fields = recording_redis.messages["event-contract-v1"][0]
    fields = {key: str(value) for key, value in raw_fields.items()}
    envelope = ExecutionStreamEnvelope.from_redis_fields(fields)

    assert set(fields) == {
        "event_id",
        "event_type",
        "schema_version",
        "execution_id",
        "payload",
        "occurred_at",
    }
    assert envelope.event_id == event.id
    assert envelope.execution_id == execution.id
    assert envelope.schema_version == "1.0"
    assert envelope.payload == event.payload
    assert json.loads(fields["payload"]) == event.payload

    async with session_factory() as session:
        stored = await session.scalar(
            select(OutboxEventORM).where(OutboxEventORM.id == event.id)
        )
    assert stored is not None
    assert "schema_version" not in stored.payload


def test_stream_envelope_rejects_wrong_version_and_legacy_fields() -> None:
    execution_id = uuid4()
    event = build_execution_event(
        execution_id=execution_id,
        event_type="execution.started",
        payload=_started_payload(),
    )
    fields = {
        "event_id": str(event.id),
        "event_type": event.event_type,
        "schema_version": "2.0",
        "execution_id": str(execution_id),
        "occurred_at": event.created_at.isoformat(),
        "payload": json.dumps(event.payload),
    }
    with pytest.raises(ValidationError):
        ExecutionStreamEnvelope.from_redis_fields(fields)

    fields["schema_version"] = "1.0"
    fields["aggregate_type"] = "Execution"
    with pytest.raises(ValidationError):
        ExecutionStreamEnvelope.from_redis_fields(fields)


def test_successful_step_requires_persisted_result() -> None:
    with pytest.raises(ValidationError, match="persisted result"):
        build_execution_event(
            execution_id=uuid4(),
            event_type="execution.step_completed",
            payload={
                "status": "SUCCEEDED",
                "operation": {"id": str(uuid4()), "number": 1},
                "step": {"id": str(uuid4()), "sequence": 0},
                "attempt": {
                    "id": str(uuid4()),
                    "number": 1,
                    "reason": "INITIAL",
                },
                "result_ref": None,
                "output_summary": None,
                "error": None,
            },
        )
