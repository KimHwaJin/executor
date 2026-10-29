"""Tenant-scoped Execution submission, listing, and event Streams."""

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    ExecutionStatus,
    OperationMode,
    RuntimeType,
    TriggerType,
)
from executor_service.domain.errors import (
    IdempotencyConflictError,
    InvalidExecutionSpecError,
)
from executor_service.domain.runtime import RuntimeOffering
from executor_service.domain.tenants import tenant_event_stream
from executor_service.infrastructure.db.models import (
    ExecutionEventORM,
    ExecutionORM,
    OutboxEventORM,
)
from executor_service.infrastructure.db.repositories import (
    SQLAlchemyUnitOfWork,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.event_retention import (
    EventRetentionManager,
)
from executor_service.infrastructure.events import (
    ExecutionCompletedPayload,
    ExecutionStreamEnvelope,
    build_execution_event,
)
from executor_service.infrastructure.execution_queries import (
    SQLAlchemyExecutionQueryService,
)
from executor_service.infrastructure.execution_worker.event_writer.completion import (
    add_execution_completed_event,
)
from executor_service.infrastructure.outbox import OutboxPublisher
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
)
from executor_service.infrastructure.runtime_adapters import (
    JupyterRuntimeAdapter,
)
from executor_service.settings import Settings

EVENT_STREAM = "tenant-events"


class RecordingRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    async def xadd(
        self,
        stream: str,
        fields: dict[FieldT, EncodableT],
        **options: Any,
    ) -> str:
        self.calls.append(
            (
                stream,
                {str(key): str(value) for key, value in fields.items()},
                options,
            )
        )
        return "1-0"


def _command(
    *,
    key: str | None = None,
    tenant_id: str = "acme-data",
) -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=key or f"tenant-{uuid4().hex}",
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="tenant-user",
        project_id=None,
        session_id=None,
        tenant_id=tenant_id,
        steps=(StepSpec(sequence=0, code="print('tenant')"),),
    )


def _service(engine: AsyncEngine, tmp_path: Path) -> ExecutionService:
    session_factory = create_session_factory(engine)
    return ExecutionService(
        lambda: SQLAlchemyUnitOfWork(session_factory),
        {
            RuntimeType.JUPYTER: RuntimeOffering(
                ("basic",), JupyterRuntimeAdapter.capabilities
            )
        },
        FilesystemExecutionResultStore(tmp_path),
    )


async def _publish_started_event(
    engine: AsyncEngine,
    execution_id: Any,
    *,
    maxlen: int | None = None,
) -> RecordingRedis:
    event = build_execution_event(
        execution_id=execution_id,
        event_sequence=1,
        event_type="execution.started",
        payload={
            "status": "RUNNING",
            "runtime": {
                "provider": "JUPYTER",
                "profile": "basic",
                "target_id": str(uuid4()),
            },
        },
    )
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(ExecutionEventORM.from_domain(event))
        session.add(OutboxEventORM.from_execution_event(event))
    redis = RecordingRedis()
    publisher = OutboxPublisher(
        session_factory=session_factory,
        redis=cast(Redis, redis),
        event_stream_name=EVENT_STREAM,
        poll_interval_seconds=0.01,
        batch_size=10,
        event_stream_maxlen=maxlen,
    )
    assert await publisher.publish_batch() == 1
    return redis


def _event_calls(
    redis: RecordingRedis,
) -> list[tuple[str, dict[str, str], dict[str, Any]]]:
    return redis.calls


async def test_tenant_events_use_their_own_stream_and_envelope(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    execution = await _service(engine, tmp_path).submit(
        _command(tenant_id="acme-data")
    )

    redis = await _publish_started_event(engine, execution.id)

    [(stream, fields, options)] = _event_calls(redis)
    assert stream == "tenant-events.acme-data"
    assert options == {}
    assert fields["tenant_id"] == "acme-data"
    envelope = ExecutionStreamEnvelope.from_redis_fields(fields)
    assert envelope.tenant_id == "acme-data"
    assert "tenant_id" not in envelope.payload


async def test_event_maxlen_trims_approximately(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    execution = await _service(engine, tmp_path).submit(
        _command(tenant_id="acme-data")
    )

    redis = await _publish_started_event(engine, execution.id, maxlen=5000)

    by_stream = {stream: options for stream, _, options in redis.calls}
    assert by_stream["tenant-events.acme-data"] == {
        "maxlen": 5000,
        "approximate": True,
    }


def test_maxlen_setting_reads_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REDIS_EVENT_STREAM_MAXLEN", "100000")
    assert Settings(_env_file=None).redis_event_stream_maxlen == 100000
    monkeypatch.delenv("REDIS_EVENT_STREAM_MAXLEN")
    assert Settings(_env_file=None).redis_event_stream_maxlen is None


@pytest.mark.parametrize(
    "tenant_id",
    [
        "ab",
        "Acme",
        "1acme",
        "acme-",
        "acme_data",
        "acme.data",
        "a" * 65,
        "dlq",
        "work",
    ],
)
async def test_invalid_or_reserved_tenant_is_rejected(
    engine: AsyncEngine, tmp_path: Path, tenant_id: str
) -> None:
    with pytest.raises(InvalidExecutionSpecError, match="tenant_id"):
        await _service(engine, tmp_path).submit(_command(tenant_id=tenant_id))


def test_tenant_stream_is_below_the_event_stream() -> None:
    assert tenant_event_stream("a.events", "acme") == "a.events.acme"


async def test_tenant_is_part_of_the_idempotency_fingerprint(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    service = _service(engine, tmp_path)
    command = _command(key="tenant-fingerprint", tenant_id="acme-data")
    first = await service.submit(command)

    assert (await service.submit(command)).id == first.id
    with pytest.raises(IdempotencyConflictError):
        await service.submit(replace(command, tenant_id="other-team"))


async def test_tenant_execution_completes_with_valid_event(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    execution = await _service(engine, tmp_path).submit(
        _command(tenant_id="acme-data")
    )
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(status=ExecutionStatus.SUCCEEDED)
        )
        await add_execution_completed_event(session, execution.id)

    async with session_factory() as session:
        event = await session.scalar(
            select(ExecutionEventORM).where(
                ExecutionEventORM.execution_id == execution.id,
                ExecutionEventORM.event_type == "execution.completed",
            )
        )
    assert event is not None
    payload = ExecutionCompletedPayload.model_validate(event.payload)
    assert payload.execution is not None
    assert payload.execution.context.user_id == "tenant-user"


async def test_query_service_filters_by_tenant(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    service = _service(engine, tmp_path)
    acme = await service.submit(_command(tenant_id="acme-data"))
    await service.submit(_command(tenant_id="other-team"))
    await service.submit(_command(tenant_id="third-team"))
    queries = SQLAlchemyExecutionQueryService(create_session_factory(engine))

    page = await queries.executions(tenant_id="acme-data")

    assert [item.id for item in page.items] == [acme.id]
    assert page.items[0].tenant_id == "acme-data"
    assert (await queries.execution(acme.id)).tenant_id == "acme-data"
    assert len((await queries.executions()).items) == 3


async def test_retention_covers_every_tenant_stream(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    service = _service(engine, tmp_path)
    await service.submit(_command(tenant_id="acme-data"))
    await service.submit(_command(tenant_id="acme-data"))
    await service.submit(_command(tenant_id="other-team"))
    settings = Settings(_env_file=None, redis_event_stream=EVENT_STREAM)
    manager = EventRetentionManager(
        create_session_factory(engine), cast(Redis, None), settings
    )

    assert await manager._event_streams() == [
        f"{EVENT_STREAM}.acme-data",
        f"{EVENT_STREAM}.other-team",
    ]


@pytest.mark.redis
async def test_real_redis_tenant_stream_is_capped(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    client = Redis.from_url(
        os.getenv("EXECUTOR_REDIS_TEST_URL", "redis://127.0.0.1:6379/15"),
        decode_responses=True,
    )
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        if os.getenv("EXECUTOR_REQUIRE_REDIS_TESTS") == "1":
            raise
        pytest.skip()
    base = f"test:tenant:{uuid4().hex}"
    stream = f"{base}.acme-data"
    execution = await _service(engine, tmp_path).submit(
        _command(tenant_id="acme-data")
    )
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        for sequence in range(1, 301):
            event = build_execution_event(
                execution_id=execution.id,
                event_sequence=sequence,
                event_type="execution.started",
                payload={
                    "status": "RUNNING",
                    "runtime": {
                        "provider": "JUPYTER",
                        "profile": "basic",
                        "target_id": str(uuid4()),
                    },
                },
            )
            session.add(ExecutionEventORM.from_domain(event))
            session.add(OutboxEventORM.from_execution_event(event))
    publisher = OutboxPublisher(
        session_factory=session_factory,
        redis=client,
        event_stream_name=base,
        poll_interval_seconds=0.01,
        batch_size=1000,
        event_stream_maxlen=10,
    )
    try:
        assert await publisher.publish_batch() == 300
        assert not await client.exists(base)
        # MAXLEN ~ trims whole macro nodes, so a few extra entries remain.
        assert 10 <= await client.xlen(stream) < 300
        [(_, fields)] = await client.xrevrange(stream, count=1)
        envelope = ExecutionStreamEnvelope.from_redis_fields(fields)
        assert envelope.tenant_id == "acme-data"
        assert envelope.event_sequence == 300
    finally:
        await client.delete(base, stream)
        await client.aclose()
