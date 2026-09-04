import asyncio
import json
import os
from collections.abc import AsyncIterator, Coroutine
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    AttemptStatus,
    OperationMode,
    RuntimePool,
    RuntimeTargetStatus,
    TriggerType,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.event_retention import (
    EventRetentionManager,
)
from executor_service.infrastructure.execution_worker import ExecutionWorker
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.settings import Settings
from tests.runtime_credentials import runtime_credential_fields

pytestmark = pytest.mark.redis


def test_dead_letter_stream_must_be_separate() -> None:
    with pytest.raises(ValueError, match="must be distinct"):
        Settings(
            redis_work_stream="same-stream", redis_event_stream="same-stream"
        )


async def test_retention_trims_old_entries_without_removing_work_pending(
    redis_client: Redis,
    engine: AsyncEngine,
) -> None:
    unique = uuid4().hex
    work_stream = f"test:retention:{unique}:work"
    event_stream = f"test:retention:{unique}:events"
    work_dlq = f"test:retention:{unique}:work-dlq"
    group = f"test:retention:{unique}:workers"
    old_ms = int((utc_now() - timedelta(days=4)).timestamp() * 1000)
    now_ms = int(utc_now().timestamp() * 1000)
    old_ids = [f"{old_ms}-{index}" for index in range(250)]
    recent_id = f"{now_ms}-0"
    settings = Settings(
        runtime_enabled=False,
        redis_work_stream=work_stream,
        redis_event_stream=event_stream,
        redis_work_dead_letter_stream=work_dlq,
        redis_event_dead_letter_stream=f"{work_dlq}:agent-owned",
        redis_work_retention_seconds=3 * 24 * 3600,
        redis_event_retention_seconds=3 * 24 * 3600,
        redis_work_dlq_retention_seconds=3 * 24 * 3600,
    )
    manager = EventRetentionManager(
        create_session_factory(engine), redis_client, settings
    )
    try:
        for message_id in old_ids:
            await redis_client.xadd(
                work_stream, {"value": "old"}, id=message_id
            )
            await redis_client.xadd(
                event_stream, {"value": "old"}, id=message_id
            )
            await redis_client.xadd(work_dlq, {"value": "old"}, id=message_id)
        await redis_client.xadd(work_stream, {"value": "recent"}, id=recent_id)
        await redis_client.xadd(
            event_stream, {"value": "recent"}, id=recent_id
        )
        await redis_client.xadd(work_dlq, {"value": "recent"}, id=recent_id)
        await redis_client.xgroup_create(work_stream, group, id="0")
        delivered = await redis_client.xreadgroup(
            group,
            "consumer-a",
            streams={work_stream: ">"},
            count=250,
        )
        delivered_ids = [message_id for message_id, _ in delivered[0][1]]
        await redis_client.xack(work_stream, group, *delivered_ids[:200])

        await manager._trim_work_stream()
        await manager._trim_by_age(
            event_stream, settings.redis_event_retention_seconds
        )
        await manager._trim_by_age(
            work_dlq, settings.redis_work_dlq_retention_seconds
        )

        pending = await redis_client.xpending_range(
            work_stream, group, "-", "+", 100
        )
        assert len(pending) == 50
        assert await redis_client.xrange(
            work_stream, min=delivered_ids[200], max=delivered_ids[200]
        )
        assert await redis_client.xrange(
            event_stream, min=recent_id, max=recent_id
        )
        assert await redis_client.xrange(
            work_dlq, min=recent_id, max=recent_id
        )
        assert await redis_client.xlen(event_stream) < 251
        assert await redis_client.xlen(work_dlq) < 251
    finally:
        await redis_client.delete(work_stream, event_stream, work_dlq)


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    redis_url = os.getenv(
        "EXECUTOR_REDIS_TEST_URL", "redis://127.0.0.1:6379/15"
    )
    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:
        await client.aclose()
        if os.getenv("EXECUTOR_REQUIRE_REDIS_TESTS") == "1":
            raise RuntimeError(
                f"Required Redis integration server is unavailable: {exc}"
            ) from exc
        pytest.skip()
    try:
        yield client
    finally:
        await client.aclose()


def _worker(
    engine: AsyncEngine,
    redis: Redis,
    tmp_path: Path,
    *,
    stream: str,
    dlq_stream: str,
    group: str,
    consumer: str,
) -> ExecutionWorker:
    settings = Settings(
        runtime_enabled=False,
        shared_storage_root=tmp_path,
        redis_work_stream=stream,
        redis_work_dead_letter_stream=dlq_stream,
        execution_consumer_group=group,
        execution_consumer_name=consumer,
        execution_pending_claim_idle_milliseconds=1,
        execution_pending_claim_batch_size=10,
    )
    session_factory = create_session_factory(engine)
    worker = ExecutionWorker(
        session_factory=session_factory,
        redis=redis,
        settings=settings,
        registry=RuntimeTargetRegistry(session_factory, settings),
        artifact_manager=ExecutionArtifactManager(session_factory),
    )
    # These tests exercise active-Worker internals without starting background loops.
    worker._dispatcher.set_accepting(True)
    worker._stopped = False
    return worker


def _work_fields(execution_id: UUID) -> dict[str, str]:
    return {
        "message_id": str(uuid4()),
        "message_type": "operation.ready",
        "schema_version": "1.0",
        "aggregate_type": "Execution",
        "aggregate_id": str(execution_id),
        "occurred_at": "2026-08-09T00:00:00+00:00",
        "payload": json.dumps(
            {
                "schema_version": "1.0",
                "execution_id": str(execution_id),
                "operation_id": str(uuid4()),
            }
        ),
    }


def _redis_fields(fields: dict[str, str]) -> dict[FieldT, EncodableT]:
    return cast(dict[FieldT, EncodableT], fields)


def _command() -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=f"event-delivery-{uuid4().hex}",
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="event-user",
        project_id="event-project",
        session_id="event-session",
        task_id="test-task",
        steps=(
            StepSpec(
                sequence=0,
                code="print('claim once')",
                tool_name="claim_once",
            ),
        ),
    )


async def test_stale_pending_message_is_reclaimed_and_acked_once(
    engine: AsyncEngine,
    redis_client: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().hex
    stream = f"test:executor:{suffix}"
    dlq_stream = f"{stream}:dlq"
    group = f"test-workers-{suffix}"
    execution_id = uuid4()
    worker = _worker(
        engine,
        redis_client,
        tmp_path,
        stream=stream,
        dlq_stream=dlq_stream,
        group=group,
        consumer="replacement-worker",
    )
    dispatched: list[UUID] = []

    def record_dispatch(
        dispatched_execution_id: UUID,
        coroutine: Coroutine[Any, Any, None],
        *,
        replace: bool = False,
    ) -> None:
        del replace
        dispatched.append(dispatched_execution_id)
        coroutine.close()

    monkeypatch.setattr(worker._dispatcher, "dispatch", record_dispatch)
    try:
        await redis_client.xgroup_create(stream, group, id="0", mkstream=True)
        await redis_client.xadd(
            stream, _redis_fields(_work_fields(execution_id))
        )
        delivered = await redis_client.xreadgroup(
            groupname=group,
            consumername="dead-worker",
            streams={stream: ">"},
            count=1,
        )
        assert delivered
        await asyncio.sleep(0.01)

        assert await worker._stream_consumer.recover_pending_messages() == 1
        assert await worker._stream_consumer.recover_pending_messages() == 0
        assert dispatched == [execution_id]
        assert (
            await redis_client.xpending_range(stream, group, "-", "+", 10)
            == []
        )
    finally:
        await redis_client.delete(stream, dlq_stream)


@pytest.mark.parametrize(
    ("invalid_fields", "expected_reason"),
    [
        ({"message_id": "not-a-uuid"}, "invalid_message_id"),
        ({"aggregate_type": "UnknownAggregate"}, "unsupported_aggregate_type"),
        (
            {"message_type": "TOP-SECRET-event-family"},
            "unsupported_message_type",
        ),
        ({"schema_version": "2.0"}, "unsupported_schema_version"),
        ({"payload": "not-json"}, "invalid_work_message_contract"),
    ],
)
async def test_invalid_message_is_safely_dead_lettered(
    engine: AsyncEngine,
    redis_client: Redis,
    tmp_path: Path,
    invalid_fields: dict[str, str],
    expected_reason: str,
) -> None:
    suffix = uuid4().hex
    stream = f"test:executor:{suffix}"
    dlq_stream = f"{stream}:dlq"
    group = f"test-workers-{suffix}"
    fields = _work_fields(uuid4()) | invalid_fields
    worker = _worker(
        engine,
        redis_client,
        tmp_path,
        stream=stream,
        dlq_stream=dlq_stream,
        group=group,
        consumer="dlq-worker",
    )
    try:
        await redis_client.xgroup_create(stream, group, id="0", mkstream=True)
        message_id = await redis_client.xadd(stream, _redis_fields(fields))
        delivered = await redis_client.xreadgroup(
            groupname=group,
            consumername="dlq-worker",
            streams={stream: ">"},
            count=1,
        )
        assert delivered
        await worker._stream_consumer.process_message(message_id, fields)

        assert (
            await redis_client.xpending_range(stream, group, "-", "+", 10)
            == []
        )
        dead_letters = await redis_client.xrange(dlq_stream)
        assert len(dead_letters) == 1
        _, dead_letter = dead_letters[0]
        assert dead_letter["source_message_id"] == message_id
        assert dead_letter["reason"] == expected_reason
        assert "payload" not in dead_letter
        assert "must-not-enter-dlq" not in repr(dead_letter)
        assert "TOP-SECRET" not in repr(dead_letter)
    finally:
        await redis_client.delete(stream, dlq_stream)


async def test_duplicate_dispatch_keeps_one_active_job(
    engine: AsyncEngine,
    redis_client: Redis,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().hex
    stream = f"test:executor:{suffix}"
    execution_id = uuid4()
    worker = _worker(
        engine,
        redis_client,
        tmp_path,
        stream=stream,
        dlq_stream=f"{stream}:dlq",
        group=f"test-workers-{suffix}",
        consumer="duplicate-worker",
    )
    started = asyncio.Event()
    release = asyncio.Event()
    invocations = 0

    async def blocking_execution(_execution_id: UUID) -> None:
        nonlocal invocations
        invocations += 1
        started.set()
        await release.wait()

    monkeypatch.setattr(worker._runner, "run", blocking_execution)
    fields = _work_fields(execution_id)
    await worker._work_admission.handle_message(fields)
    await started.wait()
    await worker._work_admission.handle_message(fields)
    assert worker.active_job_count == 1
    assert invocations == 1

    release.set()
    await worker._dispatcher.wait_idle()
    await asyncio.sleep(0)
    assert worker.active_job_count == 0


async def test_two_workers_create_only_one_execution_attempt(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    redis_client: Redis,
    tmp_path: Path,
) -> None:
    execution = await execution_service.submit(_command())
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        session.add(
            RuntimeTargetORM(
                name=f"claim-target-{uuid4().hex}",
                connection_config={"endpoint": "http://127.0.0.1:9"},
                **runtime_credential_fields(),
                pool=RuntimePool.INTERACTIVE,
                status=RuntimeTargetStatus.ACTIVE,
                max_concurrent_executions=2,
                supported_profiles=["basic"],
                enabled=True,
            )
        )

    suffix = uuid4().hex
    first_worker = _worker(
        engine,
        redis_client,
        tmp_path,
        stream=f"test:executor:{suffix}",
        dlq_stream=f"test:executor:{suffix}:dlq",
        group=f"test-workers-{suffix}",
        consumer="first-worker",
    )
    second_worker = _worker(
        engine,
        redis_client,
        tmp_path,
        stream=f"test:executor:{suffix}",
        dlq_stream=f"test:executor:{suffix}:dlq",
        group=f"test-workers-{suffix}",
        consumer="second-worker",
    )

    assert await first_worker._claimer.claim(execution.id) is not None
    assert await second_worker._claimer.claim(execution.id) is None
    async with session_factory() as session:
        attempts = await session.scalar(
            select(func.count(ExecutionAttemptORM.id)).where(
                ExecutionAttemptORM.execution_id == execution.id,
                ExecutionAttemptORM.status == AttemptStatus.RUNNING,
            )
        )
    assert attempts == 1
