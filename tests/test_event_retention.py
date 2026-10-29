from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis
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
    OutboxStatus,
    TriggerType,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionEventORM,
    ExecutionORM,
    OutboxEventORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.event_retention import (
    EventRetentionManager,
)
from executor_service.infrastructure.events import build_execution_event
from executor_service.infrastructure.execution_queries import (
    SQLAlchemyExecutionQueryService,
)
from executor_service.settings import Settings


class RecordingRetentionRedis:
    """Passes the compatibility check and records retention trims."""

    def __init__(self, trimmed_per_call: int = 0) -> None:
        self.trim_calls: list[tuple[Any, ...]] = []
        self.trimmed_per_call = trimmed_per_call

    async def info(self, _section: str) -> dict[str, str]:
        return {"redis_version": "6.0.8"}

    async def execute_command(self, *args: Any) -> Any:
        if args[0] == "COMMAND":
            return {name: {"name": name} for name in args[2:]}
        if args[:2] == ("EVAL", "return 1"):
            return 1
        self.trim_calls.append(args)
        return self.trimmed_per_call


class UnreachableRedis:
    async def info(self, _section: str) -> dict[str, str]:
        raise ConnectionError("Redis is down")


def _command(key: str) -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=key,
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="unscoped",
        project_id=None,
        session_id=None,
        tenant_id="test-tenant",
        steps=(StepSpec(sequence=0, code="print('retention')"),),
    )


def _settings() -> Settings:
    return Settings(
        runtime_enabled=False,
        event_retention_interval_seconds=3600,
        redis_event_retention_seconds=7 * 24 * 3600,
        published_outbox_retention_seconds=7 * 24 * 3600,
        execution_event_retention_seconds=90 * 24 * 3600,
    )


async def test_published_outbox_expires_before_durable_event_history(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(_command("retention-history"))
    now = utc_now()
    occurred_at = now - timedelta(days=10)
    event = build_execution_event(
        execution_id=execution.id,
        event_sequence=1,
        event_type="execution.started",
        payload={
            "status": "RUNNING",
            "runtime": {
                "provider": "JUPYTER",
                "profile": "basic",
                "target_id": str(uuid4()),
                "session_id": "retention-session",
            },
        },
    )
    event.created_at = occurred_at
    event.updated_at = occurred_at
    outbox = OutboxEventORM.from_execution_event(event)
    outbox.status = OutboxStatus.PUBLISHED
    outbox.published_at = occurred_at
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.SUCCEEDED,
                finished_at=occurred_at,
            )
        )
        session.add(ExecutionEventORM.from_domain(event))
        session.add(outbox)

    redis = RecordingRetentionRedis()
    manager = EventRetentionManager(
        session_factory, cast(Redis, redis), _settings()
    )
    result = await manager.run_pass()

    assert result.published_outbox_deleted == 1
    assert result.execution_events_deleted == 0
    async with session_factory() as session:
        durable = await session.get(ExecutionEventORM, event.id)
        delivery = await session.get(OutboxEventORM, outbox.id)
    assert durable is not None
    assert delivery is None

    page = await SQLAlchemyExecutionQueryService(session_factory).events(
        execution.id
    )
    assert [item.event_sequence for item in page] == [1]
    assert page.items[0].delivery_status is None


async def test_terminal_event_history_expires_only_without_outbox_reference(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(_command("retention-terminal"))
    now = utc_now()
    occurred_at = now - timedelta(days=100)
    removable = build_execution_event(
        execution_id=execution.id,
        event_sequence=1,
        event_type="execution.started",
        payload={
            "status": "RUNNING",
            "runtime": {
                "provider": "JUPYTER",
                "profile": "basic",
                "target_id": str(uuid4()),
                "session_id": "old-session",
            },
        },
    )
    removable.created_at = occurred_at
    removable.updated_at = occurred_at
    retained = build_execution_event(
        execution_id=execution.id,
        event_sequence=2,
        event_type="execution.started",
        payload={
            "status": "RUNNING",
            "runtime": {
                "provider": "JUPYTER",
                "profile": "basic",
                "target_id": str(uuid4()),
                "session_id": "pending-session",
            },
        },
    )
    retained.created_at = occurred_at
    retained.updated_at = occurred_at
    pending_outbox = OutboxEventORM.from_execution_event(retained)
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ExecutionORM)
            .where(ExecutionORM.id == execution.id)
            .values(
                status=ExecutionStatus.SUCCEEDED,
                finished_at=occurred_at,
            )
        )
        session.add_all(
            [
                ExecutionEventORM.from_domain(removable),
                ExecutionEventORM.from_domain(retained),
                pending_outbox,
            ]
        )

    manager = EventRetentionManager(
        session_factory,
        cast(Redis, RecordingRetentionRedis()),
        _settings(),
    )
    result = await manager.run_pass()

    assert result.execution_events_deleted == 1
    async with session_factory() as session:
        remaining = list(
            await session.scalars(
                select(ExecutionEventORM).where(
                    ExecutionEventORM.execution_id == execution.id
                )
            )
        )
    assert [event.id for event in remaining] == [retained.id]


async def test_event_trim_is_bounded(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    await execution_service.submit(_command("retention-trim"))
    session_factory = create_session_factory(engine)
    redis = RecordingRetentionRedis()
    manager = EventRetentionManager(
        session_factory, cast(Redis, redis), _settings()
    )

    result = await manager.run_pass()
    assert result.published_outbox_deleted == 0
    assert len(redis.trim_calls) == 1
    assert redis.trim_calls[0][0] == "EVAL"
    assert redis.trim_calls[0][3] == "executor.events.test-tenant"
    assert redis.trim_calls[0][-1] == 1000


async def test_pass_drains_a_backlog_larger_than_one_batch(
    execution_service: ExecutionService,
    engine: AsyncEngine,
) -> None:
    execution = await execution_service.submit(_command("retention-backlog"))
    expired = utc_now() - timedelta(days=10)
    session_factory = create_session_factory(engine)
    async with session_factory() as session, session.begin():
        for sequence in range(1, 8):
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
            outbox = OutboxEventORM.from_execution_event(event)
            outbox.status = OutboxStatus.PUBLISHED
            outbox.published_at = expired
            session.add(outbox)
    redis = RecordingRetentionRedis()
    settings = _settings().model_copy(update={"event_retention_batch_size": 3})
    manager = EventRetentionManager(
        session_factory, cast(Redis, redis), settings
    )

    result = await manager.run_pass()

    assert result.published_outbox_deleted == 7
    async with session_factory() as session:
        assert not list(await session.scalars(select(OutboxEventORM.id)))


async def test_pass_stops_at_its_time_budget(
    execution_service: ExecutionService,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await execution_service.submit(_command("retention-budget"))
    monkeypatch.setattr(
        "executor_service.infrastructure.event_retention."
        "RETENTION_PASS_BUDGET_SECONDS",
        0,
    )
    redis = RecordingRetentionRedis(trimmed_per_call=1000)
    manager = EventRetentionManager(
        create_session_factory(engine), cast(Redis, redis), _settings()
    )

    result = await manager.run_pass()

    # Full batches keep coming, but the spent budget ends the pass.
    assert len(redis.trim_calls) == 1
    assert result.event_stream_trimmed == 1000


async def test_pass_cleans_postgresql_while_redis_is_down(
    engine: AsyncEngine,
) -> None:
    manager = EventRetentionManager(
        create_session_factory(engine),
        cast(Redis, UnreachableRedis()),
        _settings(),
    )

    result = await manager.run_pass()

    assert result.event_stream_trimmed == 0
    assert manager._redis_checked is False
