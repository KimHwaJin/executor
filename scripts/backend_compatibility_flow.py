"""Real application lifecycle used by backend_compatibility_check.py.

Only call with an isolated migrated schema and run-owned Redis Streams.
No runtime server is registered and no user code is executed.
"""

from datetime import timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from executor_service.application.commands import (
    CancelExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    ActorType,
    ExecutionStatus,
    OperationMode,
    OperationStatus,
    OutboxStatus,
    RuntimeType,
    StepStatus,
    TriggerType,
)
from executor_service.domain.models import utc_now
from executor_service.events import ExecutionStreamEnvelope
from executor_service.infrastructure.db.models import (
    ExecutionEventORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepORM,
    OutboxEventORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.db.repositories import (
    SQLAlchemyUnitOfWork,
)
from executor_service.infrastructure.db.session import create_session_factory
from executor_service.infrastructure.event_retention import (
    EventRetentionManager,
)
from executor_service.infrastructure.execution_worker.cancellation import (
    CancellationProcessor,
)
from executor_service.infrastructure.execution_worker.claiming import (
    ExecutionClaimer,
)
from executor_service.infrastructure.execution_worker.lease_heartbeat import (
    LeaseHeartbeatManager,
)
from executor_service.infrastructure.execution_worker.runtime_calls import (
    RuntimeDriverProvider,
)
from executor_service.infrastructure.execution_worker.stream_consumer import (
    WorkStreamConsumer,
)
from executor_service.infrastructure.execution_worker.target_selector import (
    RuntimeTargetSelector,
)
from executor_service.infrastructure.maintenance import (
    ExecutorMaintenanceService,
)
from executor_service.infrastructure.outbox import OutboxPublisher
from executor_service.infrastructure.result_storage import (
    FilesystemExecutionResultStore,
)
from executor_service.infrastructure.runtime_drivers import (
    ConfiguredRuntimeDriverFactory,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.settings import Settings
from executor_service.work_messages import WorkStreamEnvelope


class FlowFailure(Exception):
    """Safe fixed-message assertion, never populated from driver exceptions."""


def verify(condition: object, message: str) -> None:
    if not condition:
        raise FlowFailure(message)


async def lifecycle_check(
    engine: AsyncEngine, redis: Redis, settings: Settings
) -> dict[str, Any]:
    sessions = create_session_factory(engine)
    async with sessions() as session:
        verify(
            await session.scalar(select(RuntimeTargetORM.id)) is None,
            "Test schema must not contain any runtime targets.",
        )
        verify(
            await session.scalar(select(ExecutionORM.id)) is None,
            "Test schema must not contain any executions.",
        )
    await ExecutorMaintenanceService(sessions).initialize()
    service = ExecutionService(
        lambda: SQLAlchemyUnitOfWork(sessions),
        {RuntimeType.JUPYTER: ("default",)},
        FilesystemExecutionResultStore(settings.shared_storage_root),
    )
    publisher = OutboxPublisher(
        sessions,
        redis,
        settings.redis_work_stream,
        settings.redis_event_stream,
        poll_interval_seconds=0.1,
        batch_size=100,
    )
    claimer = ExecutionClaimer(
        sessions,
        settings,
        "compatibility-worker",
        RuntimeTargetSelector(settings),
    )
    cancellation = CancellationProcessor(
        sessions,
        claimer,
        LeaseHeartbeatManager(sessions, settings),
        RuntimeDriverProvider(
            RuntimeTargetRegistry(sessions, settings),
            ConfiguredRuntimeDriverFactory(settings),
        ),
    )
    handled: list[str] = []

    async def handle(fields: dict[str, str]) -> bool:
        message = WorkStreamEnvelope.from_redis_fields(fields)
        if message.message_type == "operation.ready":
            work = await claimer.claim(message.aggregate_id)
            verify(
                work is None, "Unexpected runtime assignment in isolated DB."
            )
        elif message.message_type == "execution.cancellation_ready":
            await cancellation.cancel(message.aggregate_id)
        else:
            raise FlowFailure("Unexpected work message type.")
        handled.append(message.message_type)
        return True

    consumer = WorkStreamConsumer(
        redis, settings, "compatibility-worker", handle
    )
    await consumer.ensure_group()
    await redis.xgroup_create(
        settings.redis_event_stream, "external-consumer", id="0", mkstream=True
    )

    async def publish_all() -> None:
        # Sequence ordering may require multiple batches. Do not assume one.
        for _ in range(20):
            await publisher.publish_batch()
            async with sessions() as session:
                pending = await session.scalar(
                    select(OutboxEventORM.id).where(
                        OutboxEventORM.status != OutboxStatus.PUBLISHED
                    )
                )
            if pending is None:
                return
        raise FlowFailure(
            "Outbox rows remain unpublished; check Redis write ACL."
        )

    async def consume_work() -> None:
        batches = await redis.xreadgroup(
            settings.execution_consumer_group,
            "compatibility-worker",
            {settings.redis_work_stream: ">"},
            count=100,
            block=1000,
        )
        verify(bool(batches), "Work stream did not receive an Outbox message.")
        for _stream, messages in batches:
            for message_id, fields in messages:
                await consumer.process_message(message_id, fields)
        pending = await redis.xpending(
            settings.redis_work_stream, settings.execution_consumer_group
        )
        verify(
            pending["pending"] == 0,
            "Work handler failed or ACK was denied; pending entries remain.",
        )

    command = SubmitExecutionCommand(
        idempotency_key="compat-submit",
        operation_mode=OperationMode.SINGLE,
        trigger_type=TriggerType.BATCH,
        runtime_profile="default",
        user_id="compat-user",
        project_id="compat-project",
        session_id="compat-session",
        task_id="compat-task",
        actor_type=ActorType.BATCH,
        actor_id="compatibility-check",
        steps=(StepSpec(sequence=0, code="print('compatibility')"),),
    )
    execution = await service.submit(command)
    repeated = await service.submit(command)
    verify(execution.id == repeated.id, "Submit idempotency did not hold.")
    verify(
        execution.status == ExecutionStatus.QUEUED,
        "Submitted Execution was not QUEUED.",
    )
    await publish_all()
    await consume_work()
    verify(
        handled == ["operation.ready"], "Submission work was not processed."
    )
    verify(
        (await service.get(execution.id)).status == ExecutionStatus.QUEUED,
        "No-runtime Execution did not remain QUEUED.",
    )
    cancel = CancelExecutionCommand(
        execution_id=execution.id,
        idempotency_key="compat-cancel",
        actor_type=ActorType.BATCH,
        actor_id="compatibility-check",
    )
    requested = await service.cancel(cancel)
    await service.cancel(cancel)
    verify(
        requested.status == ExecutionStatus.CANCEL_REQUESTED,
        "Cancel request did not persist CANCEL_REQUESTED.",
    )
    await publish_all()
    await consume_work()
    verify(
        handled == ["operation.ready", "execution.cancellation_ready"],
        "Cancellation work was missing or duplicated.",
    )
    await publish_all()

    async with sessions() as session:
        row = await session.get(ExecutionORM, execution.id)
        verify(
            row is not None and row.status == ExecutionStatus.CANCELLED,
            "Cancellation worker did not persist terminal state.",
        )
        steps = list(await session.scalars(select(ExecutionStepORM)))
        operations = list(await session.scalars(select(ExecutionOperationORM)))
        verify(
            len(steps) == 1 and steps[0].status == StepStatus.CANCELLED,
            "Step cancellation was not persisted.",
        )
        verify(
            len(operations) == 1
            and operations[0].status == OperationStatus.CANCELLED,
            "Operation cancellation was not persisted.",
        )
        events = list(
            await session.scalars(
                select(ExecutionEventORM).order_by(
                    ExecutionEventORM.event_sequence
                )
            )
        )
        outbox_count = len(
            list(await session.scalars(select(OutboxEventORM.id)))
        )

    batches = await redis.xreadgroup(
        "external-consumer",
        "compatibility-reader",
        {settings.redis_event_stream: ">"},
        count=100,
        block=1000,
    )
    envelopes: list[ExecutionStreamEnvelope] = []
    for _stream, messages in batches:
        for message_id, fields in messages:
            envelopes.append(ExecutionStreamEnvelope.from_redis_fields(fields))
            await redis.xack(
                settings.redis_event_stream, "external-consumer", message_id
            )
    verify(
        bool(events) and len(envelopes) == len(events),
        "Published public events do not match durable event count.",
    )
    verify(
        [event.event_sequence for event in envelopes]
        == list(range(1, len(events) + 1)),
        "Public event sequence is not contiguous for the Execution.",
    )
    for expected, actual in zip(events, envelopes, strict=True):
        verify(
            actual.event_id == expected.id
            and actual.execution_id == execution.id
            and actual.event_type == expected.event_type
            and actual.payload == expected.payload
            and actual.schema_version == expected.schema_version
            and actual.occurred_at == expected.created_at,
            "Public event fields differ from their PostgreSQL source.",
        )
    verify(
        envelopes[-1].event_type == "execution.completed"
        and envelopes[-1].payload["status"] == "CANCELLED",
        "Terminal event does not describe the cancelled Execution.",
    )
    verify(
        (
            await redis.xpending(
                settings.redis_event_stream, "external-consumer"
            )
        )["pending"]
        == 0,
        "External event consumer could not ACK all events.",
    )

    # The actual consumer must quarantine invalid work, not silently lose it.
    bad_id = await redis.xadd(settings.redis_work_stream, {"invalid": "probe"})
    await consume_work()
    dead_letters = await redis.xrange(settings.redis_work_dead_letter_stream)
    verify(
        len(dead_letters) == 1
        and dead_letters[0][1]["source_message_id"] == bad_id,
        "Invalid work was not persisted in the DLQ before ACK.",
    )

    retention = EventRetentionManager(sessions, redis, settings)
    await retention.initialize()
    verify(
        await retention.run_once() is not None,
        "Retention lease could not be acquired in isolated DB.",
    )
    # Fresh history is deliberately retained by the production policy.
    async with sessions() as session:
        verify(
            len(list(await session.scalars(select(ExecutionEventORM.id))))
            == len(events),
            "Retention unexpectedly deleted fresh events.",
        )
    expired_at = utc_now() - timedelta(
        seconds=max(
            settings.published_outbox_retention_seconds,
            settings.execution_event_retention_seconds,
        )
        + 3600
    )
    async with sessions() as session, session.begin():
        await session.execute(
            update(OutboxEventORM).values(published_at=expired_at)
        )
        await session.execute(
            update(ExecutionORM).values(finished_at=expired_at)
        )
    cleaned = await retention.run_once()
    verify(
        cleaned is not None
        and cleaned.published_outbox_deleted == outbox_count
        and cleaned.execution_events_deleted == len(events),
        "Retention did not delete expired Outbox and terminal history.",
    )
    async with sessions() as session:
        verify(
            await session.scalar(select(OutboxEventORM.id)) is None
            and await session.scalar(select(ExecutionEventORM.id)) is None,
            "Expired transport/history rows remain after retention.",
        )
    return {
        "execution_id": str(execution.id),
        "execution_status": "CANCELLED",
        "work_messages": handled,
        "outbox_rows": outbox_count,
        "event_types": [event.event_type for event in envelopes],
        "event_sequences": [event.event_sequence for event in envelopes],
        "jupyter_executed": False,
    }
