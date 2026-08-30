"""Persist ordered public Execution events and their Outbox records."""

from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.events import build_execution_event
from executor_service.infrastructure.db.models import (
    ExecutionEventORM,
    ExecutionEventSequenceORM,
    ExecutionORM,
    OutboxEventORM,
)
from executor_service.tracing import capture_trace_carrier


async def persist_execution_event(
    session: AsyncSession,
    execution_id: UUID,
    event_type: str,
    payload: dict[str, object],
) -> None:
    execution = await session.get(ExecutionORM, execution_id)
    if execution is None:
        raise ValueError(f"Execution {execution_id} was not found.")
    event_sequence = await next_execution_event_sequence(session, execution_id)
    actor_type = execution.updated_by_type or execution.created_by_type
    actor_id = execution.updated_by or execution.created_by
    carrier = capture_trace_carrier()
    event = build_execution_event(
        execution_id=execution_id,
        event_sequence=event_sequence,
        event_type=event_type,
        payload=payload,
        actor_type=actor_type,
        actor_id=actor_id,
        traceparent=carrier.traceparent,
        tracestate=carrier.tracestate,
    )
    session.add(ExecutionEventORM.from_domain(event))
    session.add(OutboxEventORM.from_execution_event(event))


async def next_execution_event_sequence(
    session: AsyncSession,
    execution_id: UUID,
) -> int:
    table = ExecutionEventSequenceORM.__table__
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        statement = postgresql_insert(ExecutionEventSequenceORM)
    elif dialect_name == "sqlite":
        statement = sqlite_insert(ExecutionEventSequenceORM)
    else:
        raise RuntimeError(
            f"Unsupported event sequence dialect: {dialect_name}"
        )
    result = await session.execute(
        statement.values(
            execution_id=execution_id,
            last_sequence=1,
        )
        .on_conflict_do_update(
            index_elements=[table.c.execution_id],
            set_={"last_sequence": table.c.last_sequence + 1},
        )
        .returning(table.c.last_sequence)
    )
    return int(result.scalar_one())
