from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

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
    OutboxDestination,
    OutboxStatus,
    TriggerType,
)
from executor_service.domain.models import utc_now
from executor_service.events import build_execution_event
from executor_service.infrastructure.db.models import (
    ExecutionEventORM,
    ExecutionORM,
    OutboxEventORM,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.event_retention import (
    EventRetentionManager,
)
from executor_service.infrastructure.execution_queries import (
    SQLAlchemyExecutionQueryService,
)
from executor_service.settings import Settings


class RecordingRetentionRedis:
    def __init__(self) -> None:
        self.trim_calls: list[tuple[Any, ...]] = []

    async def execute_command(self, *args: Any) -> int:
        self.trim_calls.append(args)
        return 0


def _command(key: str) -> SubmitExecutionCommand:
    return SubmitExecutionCommand(
        idempotency_key=key,
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.INTERACTIVE,
        runtime_profile="basic",
        user_id="unscoped",
        project_id=None,
        session_id=None,
        task_id=f"task-{key}",
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
    await manager.initialize()
    result = await manager.run_once()

    assert result is not None
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
    await manager.initialize()
    result = await manager.run_once()

    assert result is not None
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


async def test_event_trim_is_bounded_and_drops_legacy_work_rows(
    engine: AsyncEngine,
) -> None:
    session_factory = create_session_factory(engine)
    now = utc_now()
    async with session_factory() as session, session.begin():
        # A wake-up queued by an earlier release; nothing publishes it now.
        session.add(
            OutboxEventORM(
                aggregate_type="Execution",
                aggregate_id=uuid4(),
                event_type="operation.ready",
                destination=OutboxDestination.WORK,
                payload={"schema_version": "1.0"},
                status=OutboxStatus.PENDING,
                attempt_count=0,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    redis = RecordingRetentionRedis()
    manager = EventRetentionManager(
        session_factory, cast(Redis, redis), _settings()
    )

    result = await manager.run_once()
    assert result is not None
    assert result.published_outbox_deleted == 1
    assert len(redis.trim_calls) == 1
    assert redis.trim_calls[0][0] == "EVAL"
    assert redis.trim_calls[0][3] == "executor.events"
    assert redis.trim_calls[0][-1] == 1000
    async with session_factory() as session:
        assert await session.scalar(select(OutboxEventORM.id)) is None
