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
from executor_service.domain.enums import (
    OperationMode,
    OutboxDestination,
    TriggerType,
)
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
        self.messages: dict[str, dict[str, Any]] = {}

    async def xadd(self, stream: str, fields: dict[FieldT, EncodableT]) -> str:
        self.messages[stream] = {
            str(key): value for key, value in fields.items()
        }
        return "1-0"


def _submit_command() -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key="event-contract-v2",
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="event-user",
        project_id="event-project",
        session_id="event-session",
        task_id="event-task",
        steps=(
            StepSpec(
                sequence=0,
                code="print('event contract')",
            ),
        ),
    )


def test_every_supported_event_has_a_versioned_strict_payload_model() -> None:
    expected = {
        "execution.submitted",
        "execution.operation_submitted",
        "execution.finalization_requested",
        "execution.cancel_requested",
        "execution.retry_requested",
        "execution.started",
        "execution.resumed",
        "execution.step_started",
        "execution.step_succeeded",
        "execution.step_failed",
        "execution.retry_deferred",
        "execution.operation_succeeded",
        "execution.operation_failed",
        "execution.waiting_for_operation",
        "execution.artifact_registered",
        "execution.artifact_failed",
        "execution.succeeded",
        "execution.failed",
        "execution.cancelled",
        "execution.timeout_requested",
        "execution.runtime_session_cleanup_completed",
        "execution.runtime_session_cleanup_failed",
        "execution.runtime_abort_started",
        "execution.runtime_abort_completed",
        "execution.runtime_abort_failed",
        "execution.retry_window_expired",
    }

    assert set(EVENT_PAYLOAD_MODELS) == expected
    assert all(
        model.model_fields["schema_version"].default == "2.0"
        for model in EVENT_PAYLOAD_MODELS.values()
    )


def test_event_factory_rejects_unknown_missing_and_extra_fields() -> None:
    execution_id = uuid4()
    with pytest.raises(ValueError, match="Unsupported Executor event type"):
        build_execution_event(
            execution_id=execution_id,
            event_type="execution.unknown",
            payload={"status": "QUEUED"},
        )
    with pytest.raises(ValidationError):
        build_execution_event(
            execution_id=execution_id,
            event_type="execution.submitted",
            payload={"status": "QUEUED", "task_id": "task-without-plan"},
        )
    with pytest.raises(ValidationError):
        build_execution_event(
            execution_id=execution_id,
            event_type="execution.started",
            payload={"status": "RUNNING", "unexpected": True},
        )
    with pytest.raises(ValidationError):
        build_execution_event(
            execution_id=execution_id,
            event_type="execution.operation_failed",
            payload={
                "status": "FAILED",
                "execution_attempt_id": None,
                "operation_id": str(uuid4()),
                "operation_status": "FAILED",
                "first_sequence": 0,
                "last_sequence": 0,
                "version": 1,
            },
        )


async def test_postgres_outbox_and_redis_stream_share_the_same_v2_payload(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(_submit_command())
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        before = await session.scalar(
            select(OutboxEventORM).where(
                OutboxEventORM.aggregate_id == execution.id,
                OutboxEventORM.destination == OutboxDestination.EVENTS,
            )
        )
    assert before is not None
    assert before.payload["schema_version"] == EXECUTION_EVENT_SCHEMA_VERSION

    recording_redis = RecordingRedis()
    publisher = OutboxPublisher(
        session_factory=session_factory,
        redis=cast(Redis, recording_redis),
        work_stream_name="work-contract-v2",
        event_stream_name="event-contract-v2",
        poll_interval_seconds=0.01,
        batch_size=10,
        tracing=TracingManager(Settings(runtime_enabled=False)),
    )

    assert await publisher.publish_batch() == 2
    fields = {
        key: str(value)
        for key, value in recording_redis.messages["event-contract-v2"].items()
    }
    envelope = ExecutionStreamEnvelope.from_redis_fields(fields)

    assert envelope.event_id == before.id
    assert envelope.aggregate_id == execution.id
    assert envelope.schema_version == EXECUTION_EVENT_SCHEMA_VERSION
    assert envelope.payload == before.payload
    assert json.loads(fields["payload"]) == before.payload


async def test_publisher_upgrades_a_valid_pre_v1_pending_payload(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(_submit_command())
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        row = await session.scalar(
            select(OutboxEventORM).where(
                OutboxEventORM.aggregate_id == execution.id,
                OutboxEventORM.destination == OutboxDestination.EVENTS,
            )
        )
        assert row is not None
        row.payload = {
            key: value
            for key, value in row.payload.items()
            if key != "schema_version"
        }

    recording_redis = RecordingRedis()
    publisher = OutboxPublisher(
        session_factory=session_factory,
        redis=cast(Redis, recording_redis),
        work_stream_name="work-contract-v1-upgrade",
        event_stream_name="event-contract-v1-upgrade",
        poll_interval_seconds=0.01,
        batch_size=10,
        tracing=TracingManager(Settings(runtime_enabled=False)),
    )
    assert await publisher.publish_batch() == 2

    async with session_factory() as session:
        upgraded = await session.scalar(
            select(OutboxEventORM).where(
                OutboxEventORM.aggregate_id == execution.id,
                OutboxEventORM.destination == OutboxDestination.EVENTS,
            )
        )
    assert upgraded is not None
    assert upgraded.payload["schema_version"] == "2.0"
    assert (
        recording_redis.messages["event-contract-v1-upgrade"]["schema_version"]
        == "2.0"
    )


def test_stream_envelope_rejects_version_or_aggregate_mismatch() -> None:
    execution_id = uuid4()
    event = build_execution_event(
        execution_id=execution_id,
        event_type="execution.started",
        payload={"status": "RUNNING"},
    )
    fields = {
        "event_id": str(event.id),
        "event_type": event.event_type,
        "schema_version": "2.0",
        "aggregate_type": "Execution",
        "aggregate_id": str(uuid4()),
        "occurred_at": event.created_at.isoformat(),
        "payload": json.dumps(event.payload),
    }
    with pytest.raises(ValidationError, match="aggregate_id"):
        ExecutionStreamEnvelope.from_redis_fields(fields)

    fields["aggregate_id"] = str(execution_id)
    fields["schema_version"] = "1.0"
    with pytest.raises(ValidationError):
        ExecutionStreamEnvelope.from_redis_fields(fields)
