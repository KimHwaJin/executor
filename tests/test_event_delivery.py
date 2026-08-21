import asyncio
import json
from collections.abc import AsyncIterator, Coroutine
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import StepSpec, SubmitExecutionCommand
from executor_service.application.services import ExecutionService
from executor_service.config import Settings
from executor_service.domain.enums import (
    AttemptStatus,
    OperationMode,
    RuntimePool,
    RuntimeTargetStatus,
    TriggerType,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import ExecutionAttemptORM, RuntimeTargetORM
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.runtime_registry import RuntimeTargetRegistry
from executor_service.infrastructure.worker import ExecutionWorker


def test_dead_letter_stream_must_be_separate() -> None:
    with pytest.raises(ValueError, match="must be distinct"):
        Settings(redis_work_stream="same-stream", redis_event_stream="same-stream")


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    client = Redis.from_url("redis://127.0.0.1:6379/15", decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
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
        input_host_root=tmp_path,
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
    worker._accepting_work = True
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

    monkeypatch.setattr(worker, "_dispatch", record_dispatch)
    try:
        await redis_client.xgroup_create(stream, group, id="0", mkstream=True)
        await redis_client.xadd(stream, _redis_fields(_work_fields(execution_id)))
        delivered = await redis_client.xreadgroup(
            groupname=group,
            consumername="dead-worker",
            streams={stream: ">"},
            count=1,
        )
        assert delivered
        await asyncio.sleep(0.01)

        assert await worker._recover_pending_messages() == 1
        assert await worker._recover_pending_messages() == 0
        assert dispatched == [execution_id]
        assert await redis_client.xpending_range(stream, group, "-", "+", 10) == []
    finally:
        await redis_client.delete(stream, dlq_stream)


@pytest.mark.parametrize(
    ("invalid_fields", "expected_reason"),
    [
        ({"message_id": "not-a-uuid"}, "invalid_message_id"),
        ({"aggregate_type": "UnknownAggregate"}, "unsupported_aggregate_type"),
        ({"message_type": "TOP-SECRET-event-family"}, "unsupported_message_type"),
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
        await worker._process_stream_message(message_id, fields)

        assert await redis_client.xpending_range(stream, group, "-", "+", 10) == []
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

    monkeypatch.setattr(worker, "_run_execution", blocking_execution)
    fields = _work_fields(execution_id)
    await worker._handle_work_message(fields)
    await started.wait()
    await worker._handle_work_message(fields)
    assert len(worker._jobs) == 1
    assert invocations == 1

    release.set()
    await asyncio.gather(*worker._jobs.values())
    await asyncio.sleep(0)
    assert worker._jobs == {}


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
                credential_ref="settings:JUPYTER_TOKEN",
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

    assert await first_worker._claim(execution.id) is not None
    assert await second_worker._claim(execution.id) is None
    async with session_factory() as session:
        attempts = await session.scalar(
            select(func.count(ExecutionAttemptORM.id)).where(
                ExecutionAttemptORM.execution_id == execution.id,
                ExecutionAttemptORM.status == AttemptStatus.RUNNING,
            )
        )
    assert attempts == 1
