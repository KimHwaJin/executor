from collections.abc import Coroutine
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.types import Message, Receive, Scope, Send

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
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionORM,
    OutboxEventORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.outbox import OutboxPublisher
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.infrastructure.worker import ExecutionWorker
from executor_service.tracing import (
    TraceContextMiddleware,
    TracingManager,
    capture_trace_carrier,
)


class RecordingRedis:
    def __init__(self) -> None:
        self.messages: dict[str, dict[str, str]] = {}

    async def xadd(self, stream: str, fields: dict[Any, Any]) -> str:
        self.messages[stream] = {
            str(key): str(value) for key, value in fields.items()
        }
        return "1-0"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        tracing_enabled=True,
        input_host_root=tmp_path,
        runtime_enabled=False,
        execution_lease_seconds=30,
        execution_heartbeat_seconds=5,
    )


def _command() -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key="tracing-submit",
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="trace-user",
        project_id="trace-project",
        session_id="trace-session",
        task_id="test-task",
        steps=(
            StepSpec(
                sequence=0,
                code="print('sensitive generated code')",
                tool_name="trace_tool",
            ),
        ),
    )


async def test_trace_context_survives_outbox_redis_and_worker_boundary(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exporter = InMemorySpanExporter()
    settings = _settings(tmp_path)
    tracing = TracingManager(settings, span_exporter=exporter)
    session_factory = create_session_factory(engine)
    try:
        with tracing.span("agent.request"):
            execution = await execution_service.submit(_command())

        async with session_factory() as session:
            execution_row = await session.get(ExecutionORM, execution.id)
            outbox_row = await session.scalar(
                select(OutboxEventORM).where(
                    OutboxEventORM.aggregate_id == execution.id,
                    OutboxEventORM.destination == OutboxDestination.WORK,
                )
            )
        assert execution_row is not None and outbox_row is not None
        assert execution_row.traceparent is not None
        assert outbox_row.traceparent == execution_row.traceparent

        recording_redis = RecordingRedis()
        publisher = OutboxPublisher(
            session_factory=session_factory,
            redis=cast(Redis, recording_redis),
            work_stream_name="trace-work",
            event_stream_name="trace-events",
            poll_interval_seconds=0.01,
            batch_size=10,
            tracing=tracing,
        )
        assert await publisher.publish_batch() == 2
        work_fields = recording_redis.messages["trace-work"]
        assert work_fields.get("traceparent") is not None

        redis = Redis.from_url(
            "redis://127.0.0.1:6379/15", decode_responses=True
        )
        worker = ExecutionWorker(
            session_factory=session_factory,
            redis=redis,
            settings=settings,
            registry=RuntimeTargetRegistry(session_factory, settings),
            artifact_manager=ExecutionArtifactManager(session_factory),
            tracing=tracing,
        )

        def record_dispatch(
            _execution_id: UUID,
            coroutine: Coroutine[Any, Any, None],
            *,
            replace: bool = False,
        ) -> None:
            del replace
            coroutine.close()
            with tracing.span("executor.worker.dispatched"):
                pass

        monkeypatch.setattr(worker, "_dispatch", record_dispatch)
        await worker._handle_work_message(work_fields)
        await redis.aclose()
        assert await tracing.force_flush()

        spans = exporter.get_finished_spans()
        relevant = {
            span.name: span
            for span in spans
            if span.name
            in {
                "agent.request",
                "executor.outbox.publish",
                "executor.redis.consume",
                "executor.worker.dispatched",
            }
        }
        assert set(relevant) == {
            "agent.request",
            "executor.outbox.publish",
            "executor.redis.consume",
            "executor.worker.dispatched",
        }
        trace_ids = {span.context.trace_id for span in relevant.values()}
        assert len(trace_ids) == 1
        redis_parent = relevant["executor.redis.consume"].parent
        assert redis_parent is not None
        assert (
            redis_parent.span_id
            == relevant["executor.outbox.publish"].context.span_id
        )
    finally:
        await tracing.shutdown()


async def test_asgi_middleware_extracts_inbound_w3c_context(
    tmp_path: Path,
) -> None:
    exporter = InMemorySpanExporter()
    tracing = TracingManager(_settings(tmp_path), span_exporter=exporter)

    async def endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        with tracing.span("executor.mcp.test"):
            await send(
                cast(
                    Message,
                    {
                        "type": "http.response.start",
                        "status": 204,
                        "headers": [],
                    },
                )
            )
            await send(
                cast(Message, {"type": "http.response.body", "body": b""})
            )

    try:
        with tracing.span("agent.graph"):
            carrier = capture_trace_carrier()
        app = TraceContextMiddleware(endpoint, tracing)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post("/mcp", headers=carrier.as_headers())
        assert response.status_code == 204
        assert await tracing.force_flush()
        spans = {span.name: span for span in exporter.get_finished_spans()}
        assert (
            spans["agent.graph"].context.trace_id
            == spans["executor.http.request"].context.trace_id
        )
        mcp_parent = spans["executor.mcp.test"].parent
        assert mcp_parent is not None
        assert (
            mcp_parent.span_id
            == spans["executor.http.request"].context.span_id
        )
    finally:
        await tracing.shutdown()


async def test_span_attributes_and_errors_never_capture_sensitive_values(
    tmp_path: Path,
) -> None:
    exporter = InMemorySpanExporter()
    tracing = TracingManager(_settings(tmp_path), span_exporter=exporter)
    secret = "TOP-SECRET-CELL-AND-TOKEN"
    try:
        with pytest.raises(ValueError, match="TOP-SECRET"):
            with tracing.span(
                "privacy.test",
                attributes={
                    "executor.execution.id": "safe-id",
                    "executor.code": secret,
                    "db.statement": secret,
                    "jupyter.token": secret,
                    "executor.output": secret,
                },
            ):
                raise ValueError(secret)
        assert await tracing.force_flush()
        span = next(
            item
            for item in exporter.get_finished_spans()
            if item.name == "privacy.test"
        )
        serialized = repr(span.to_json())
        assert span.attributes["executor.execution.id"] == "safe-id"
        assert span.attributes["error.type"] == "ValueError"
        assert secret not in serialized
        assert span.events == ()
    finally:
        await tracing.shutdown()
