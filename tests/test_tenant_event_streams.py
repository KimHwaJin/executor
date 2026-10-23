"""Tenant-scoped Execution submission, listing, and event Streams."""

import hashlib
import json
import os
from collections.abc import AsyncIterator
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application._execution_service.support import (
    fingerprint,
)
from executor_service.application.commands import (
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.container import ApplicationContainer
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
from executor_service.domain.tenants import (
    reserved_tenant_ids,
    tenant_event_stream,
)
from executor_service.events import (
    ExecutionCompletedPayload,
    ExecutionStreamEnvelope,
    build_execution_event,
)
from executor_service.infrastructure.db.base import Base
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
from executor_service.infrastructure.execution_queries import (
    SQLAlchemyExecutionQueryService,
)
from executor_service.infrastructure.execution_worker._event_writer.completion import (
    add_execution_completed_event,
)
from executor_service.infrastructure.outbox import OutboxPublisher
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
)
from executor_service.interfaces.http.app import create_app
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
    tenant_id: str | None = None,
    task_id: str | None = "tenant-task",
) -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=key or f"tenant-{uuid4().hex}",
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="tenant-user",
        project_id=None,
        session_id=None,
        task_id=task_id,
        tenant_id=tenant_id,
        steps=(StepSpec(sequence=0, code="print('tenant')"),),
    )


def _service(
    engine: AsyncEngine,
    tmp_path: Path,
    reserved: frozenset[str] = frozenset(),
) -> ExecutionService:
    session_factory = create_session_factory(engine)
    return ExecutionService(
        lambda: SQLAlchemyUnitOfWork(session_factory),
        {RuntimeType.JUPYTER: ("basic",)},
        FilesystemExecutionResultStore(tmp_path),
        reserved_tenant_ids=reserved,
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
        work_stream_name="tenant-work",
        event_stream_name=EVENT_STREAM,
        poll_interval_seconds=0.01,
        batch_size=10,
        event_stream_maxlen=maxlen,
    )
    assert await publisher.publish_batch() == 2
    return redis


def _event_calls(
    redis: RecordingRedis,
) -> list[tuple[str, dict[str, str], dict[str, Any]]]:
    return [call for call in redis.calls if call[0] != "tenant-work"]


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


async def test_events_without_tenant_keep_the_shared_stream(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    execution = await _service(engine, tmp_path).submit(_command())

    redis = await _publish_started_event(engine, execution.id)

    [(stream, fields, _)] = _event_calls(redis)
    assert stream == EVENT_STREAM
    assert "tenant_id" not in fields
    assert ExecutionStreamEnvelope.from_redis_fields(fields).tenant_id is None


async def test_event_maxlen_trims_approximately_but_not_work(
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
    assert by_stream["tenant-work"] == {}


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


async def test_internal_stream_suffixes_are_reserved(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    settings = Settings(
        _env_file=None,
        redis_event_stream="executor.events",
        redis_work_stream="executor.events.jobs",
        redis_event_dead_letter_stream="executor.events.dlq",
    )
    assert settings.reserved_tenant_ids == {"dlq", "work", "jobs"}
    service = _service(engine, tmp_path, settings.reserved_tenant_ids)

    with pytest.raises(InvalidExecutionSpecError, match="reserved"):
        await service.submit(_command(tenant_id="jobs"))
    accepted = await service.submit(_command(tenant_id="jobs-team"))
    assert accepted.tenant_id == "jobs-team"


def test_reserved_ids_only_cover_streams_below_the_event_stream() -> None:
    assert reserved_tenant_ids("a.events", ["a.events.x", "a.work"]) == {
        "dlq",
        "work",
        "x",
    }
    assert tenant_event_stream("a.events", None) == "a.events"
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
    with pytest.raises(IdempotencyConflictError):
        await service.submit(replace(command, tenant_id=None))


def test_fingerprint_without_tenant_matches_the_pre_tenant_encoding() -> None:
    command = _command(key="legacy-fingerprint")
    payload = asdict(command)
    payload.pop("idempotency_key")
    payload.pop("tenant_id")
    legacy = hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str
        ).encode()
    ).hexdigest()

    assert fingerprint(command) == legacy
    assert fingerprint(replace(command, tenant_id="acme-data")) != legacy


async def test_execution_without_task_completes_with_valid_event(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    execution = await _service(engine, tmp_path).submit(
        _command(task_id=None, tenant_id="acme-data")
    )
    assert execution.task_id is None
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
    assert payload.execution.context.task_id is None
    assert payload.execution.context.user_id == "tenant-user"


async def test_query_service_filters_by_tenant(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    service = _service(engine, tmp_path)
    acme = await service.submit(_command(tenant_id="acme-data"))
    await service.submit(_command(tenant_id="other-team"))
    await service.submit(_command())
    queries = SQLAlchemyExecutionQueryService(create_session_factory(engine))

    page = await queries.executions(tenant_id="acme-data")

    assert [item.id for item in page.items] == [acme.id]
    assert page.items[0].tenant_id == "acme-data"
    assert (await queries.execution(acme.id)).tenant_id == "acme-data"
    assert len((await queries.executions()).items) == 3


@pytest_asyncio.fixture
async def rest_client(
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6399/15",
        runtime_enabled=False,
        shared_storage_root=tmp_path,
    )
    container = ApplicationContainer(settings)
    async with container.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    transport = httpx.ASGITransport(app=create_app(container))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client
    await container.redis.aclose()
    await container.engine.dispose()


def _submit_body(
    key: str, *, tenant_id: str | None = None, task_id: str | None = None
) -> dict[str, Any]:
    context: dict[str, Any] = {"user_id": "rest-user"}
    if task_id is not None:
        context["task_id"] = task_id
    body: dict[str, Any] = {
        "idempotency_key": key,
        "lifecycle": {"operation_mode": "SINGLE"},
        "trigger": {
            "type": "INTERACTIVE",
            "actor": {"type": "USER", "id": "rest-user"},
        },
        "runtime": {"type": "JUPYTER", "profile": "default"},
        "operation": {
            "spec": {
                "schema_version": "1.0",
                "steps": [
                    {
                        "sequence": 0,
                        "payload": {
                            "type": "PYTHON_EXECUTE",
                            "source": {
                                "type": "INLINE",
                                "content": "print('tenant')",
                            },
                        },
                    }
                ],
            }
        },
        "context": context,
    }
    if tenant_id is not None:
        body["tenant_id"] = tenant_id
    return body


async def test_rest_submit_detail_and_list_carry_tenant(
    rest_client: httpx.AsyncClient,
) -> None:
    submitted = await rest_client.post(
        "/api/v1/executions", json=_submit_body("rest-a", tenant_id="acme")
    )
    assert submitted.status_code == 202, submitted.text
    await rest_client.post(
        "/api/v1/executions",
        json=_submit_body("rest-b", task_id="rest-task"),
    )

    detail = await rest_client.get(submitted.headers["location"])
    assert detail.json()["tenant_id"] == "acme"
    assert detail.json()["context"]["task_id"] is None

    listed = await rest_client.get(
        "/api/v1/executions", params={"tenant_id": "acme"}
    )
    items = listed.json()["items"]
    assert [item["execution_id"] for item in items] == [
        submitted.json()["execution_id"]
    ]
    assert items[0]["tenant_id"] == "acme"
    everything = await rest_client.get("/api/v1/executions")
    assert {item["tenant_id"] for item in everything.json()["items"]} == {
        "acme",
        None,
    }


@pytest.mark.parametrize("tenant_id", ["Acme", "ab", "dlq", "work", ""])
async def test_rest_rejects_invalid_tenant(
    rest_client: httpx.AsyncClient, tenant_id: str
) -> None:
    body = _submit_body(f"rest-invalid-{uuid4().hex}")
    body["tenant_id"] = tenant_id
    response = await rest_client.post("/api/v1/executions", json=body)
    assert response.status_code == 422


async def test_rest_rejects_tenant_inside_context(
    rest_client: httpx.AsyncClient,
) -> None:
    body = _submit_body("rest-context-tenant")
    body["context"]["tenant_id"] = "acme"
    response = await rest_client.post("/api/v1/executions", json=body)
    assert response.status_code == 422


async def test_retention_covers_every_tenant_stream(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    service = _service(engine, tmp_path)
    await service.submit(_command(tenant_id="acme-data"))
    await service.submit(_command(tenant_id="acme-data"))
    await service.submit(_command(tenant_id="other-team"))
    await service.submit(_command())
    settings = Settings(_env_file=None, redis_event_stream=EVENT_STREAM)
    manager = EventRetentionManager(
        create_session_factory(engine), cast(Redis, None), settings
    )

    assert await manager._event_streams() == [
        EVENT_STREAM,
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
        work_stream_name=f"{base}:work",
        event_stream_name=base,
        poll_interval_seconds=0.01,
        batch_size=1000,
        event_stream_maxlen=10,
    )
    try:
        assert await publisher.publish_batch() == 301
        assert not await client.exists(base)
        # MAXLEN ~ trims whole macro nodes, so a few extra entries remain.
        assert 10 <= await client.xlen(stream) < 300
        [(_, fields)] = await client.xrevrange(stream, count=1)
        envelope = ExecutionStreamEnvelope.from_redis_fields(fields)
        assert envelope.tenant_id == "acme-data"
        assert envelope.event_sequence == 300
    finally:
        await client.delete(base, stream, f"{base}:work")
        await client.aclose()
