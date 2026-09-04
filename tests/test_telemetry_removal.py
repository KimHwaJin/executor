"""Execution delivery and failure evidence do not require telemetry."""

import ast
import asyncio
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.services import ExecutionService
from executor_service.domain.enums import OutboxStatus
from executor_service.domain.runtime import RuntimeDriverError
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionEventORM,
    ExecutionORM,
    OutboxEventORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.execution_worker import ExecutionWorker
from executor_service.infrastructure.execution_worker.runtime_calls import (
    run_runtime_operation,
)
from executor_service.infrastructure.outbox import OutboxPublisher
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.settings import Settings
from executor_service.work_messages import WorkStreamEnvelope
from tests.test_events import RecordingRedis
from tests.test_work_admission import _command


def test_executor_has_no_opentelemetry_imports_or_settings() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "executor_service"
    assert not (root / "tracing.py").exists()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("opentelemetry")
            if isinstance(node, ast.Import):
                assert all(
                    not alias.name.startswith("opentelemetry")
                    for alias in node.names
                )
    assert "tracing_enabled" not in Settings.model_fields
    assert not any(name.startswith("otel_") for name in Settings.model_fields)


@pytest.mark.parametrize(
    "model", [ExecutionORM, ExecutionEventORM, OutboxEventORM]
)
def test_schema_and_queries_have_no_trace_fields(
    model: type[ExecutionORM] | type[ExecutionEventORM] | type[OutboxEventORM],
) -> None:
    assert not {"traceparent", "tracestate"} & set(
        model.__table__.columns.keys()
    )
    statement = str(select(model))
    assert "traceparent" not in statement
    assert "tracestate" not in statement


@pytest.mark.parametrize("previous_context", [False, True])
async def test_submission_outbox_and_worker_without_trace_context(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous_context: bool,
) -> None:
    execution = await execution_service.submit(_command("no-telemetry"))
    factory = create_session_factory(engine)
    redis = RecordingRedis()
    publisher = OutboxPublisher(
        session_factory=factory,
        redis=cast(Redis, redis),
        work_stream_name="work",
        event_stream_name="events",
        poll_interval_seconds=0.01,
        batch_size=10,
    )
    assert await publisher.publish_batch() == 1
    assert await publisher.publish_batch() == 0
    fields = {k: str(v) for k, v in redis.messages["work"][0].items()}
    assert set(fields) == {
        "message_id",
        "message_type",
        "schema_version",
        "aggregate_type",
        "aggregate_id",
        "occurred_at",
        "payload",
    }
    if previous_context:
        fields.update(traceparent="previous-context", tracestate="previous")
    message = WorkStreamEnvelope.from_redis_fields(fields)
    assert message.aggregate_id == execution.id
    assert "traceparent" not in message.model_dump()
    with pytest.raises(ValidationError):
        WorkStreamEnvelope.from_redis_fields({**fields, "unexpected": "bad"})

    settings = Settings(runtime_enabled=False, shared_storage_root=tmp_path)
    client = Redis.from_url("redis://127.0.0.1:6379/15")
    worker = ExecutionWorker(
        session_factory=factory,
        redis=client,
        settings=settings,
        registry=RuntimeTargetRegistry(factory, settings),
        artifact_manager=ExecutionArtifactManager(factory),
    )
    dispatched: list[UUID] = []

    def record_dispatch(
        execution_id: UUID,
        coroutine: Coroutine[Any, Any, None],
        *,
        replace: bool = False,
    ) -> None:
        coroutine.close()
        dispatched.append(execution_id)

    monkeypatch.setattr(worker._dispatcher, "dispatch", record_dispatch)
    try:
        assert await worker._work_admission.handle_message(fields)
    finally:
        await client.aclose()
    assert dispatched == [execution.id]
    async with factory() as session:
        outbox = await session.scalar(
            select(OutboxEventORM).where(
                OutboxEventORM.aggregate_id == execution.id
            )
        )
        assert outbox is not None
        assert outbox.status == OutboxStatus.PUBLISHED


async def test_runtime_wrapper_preserves_safe_logs_and_original_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    execution_id, target_id = uuid4(), uuid4()
    error = RuntimeDriverError("Jupyter REST request failed: status=500.")

    async def fail() -> None:
        try:
            raise ValueError("password=secret-value")
        except ValueError as cause:
            raise error from cause

    with pytest.raises(RuntimeDriverError) as raised:
        await run_runtime_operation(
            "executor.runtime.code.execute",
            fail(),
            execution_id=execution_id,
            target_id=target_id,
            sequence=2,
        )
    assert raised.value is error
    record = json.loads(caplog.records[-1].getMessage())
    assert record["execution_id"] == str(execution_id)
    assert record["phase"] == "executor.runtime.code.execute"
    assert "status=500" in caplog.text
    assert "secret-value" not in caplog.text


async def test_runtime_wrapper_preserves_result_and_cancellation() -> None:
    async def succeed() -> str:
        return "result"

    async def cancelled() -> None:
        raise asyncio.CancelledError

    execution_id, target_id = uuid4(), uuid4()
    assert (
        await run_runtime_operation(
            "execute",
            succeed(),
            execution_id=execution_id,
            target_id=target_id,
        )
        == "result"
    )
    with pytest.raises(asyncio.CancelledError):
        await run_runtime_operation(
            "execute",
            cancelled(),
            execution_id=execution_id,
            target_id=target_id,
        )
