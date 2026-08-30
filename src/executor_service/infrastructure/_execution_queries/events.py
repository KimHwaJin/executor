"""SQLAlchemy reads for durable Execution Events and Outbox delivery."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.execution_queries import ExecutionEventView
from executor_service.application.pagination import (
    Page,
    decode_integer_cursor,
    encode_integer_cursor,
)
from executor_service.infrastructure._execution_queries.guards import (
    require_execution,
)
from executor_service.infrastructure._execution_queries.mappers import redact
from executor_service.infrastructure.db.models import (
    ExecutionEventORM,
    OutboxEventORM,
)


class SQLAlchemyEventReader:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def events(
        self,
        execution_id: UUID,
        *,
        after_sequence: int = 0,
        cursor: str | None = None,
        limit: int = 200,
    ) -> Page[ExecutionEventView]:
        async with self._session_factory() as session:
            await require_execution(session, execution_id)
            statement = (
                select(ExecutionEventORM, OutboxEventORM)
                .outerjoin(
                    OutboxEventORM,
                    OutboxEventORM.execution_event_id == ExecutionEventORM.id,
                )
                .where(ExecutionEventORM.execution_id == execution_id)
            )
            sequence_cursor = after_sequence
            if cursor is not None:
                sequence_cursor = decode_integer_cursor(
                    cursor, "execution_events"
                )
            statement = statement.where(
                ExecutionEventORM.event_sequence > sequence_cursor
            )
            rows = list(
                (
                    await session.execute(
                        statement.order_by(
                            ExecutionEventORM.event_sequence
                        ).limit(limit + 1)
                    )
                )
                .tuples()
                .all()
            )
        page_rows = rows[:limit]
        items = [
            ExecutionEventView(
                id=event.id,
                execution_id=event.execution_id,
                event_sequence=event.event_sequence,
                event_type=event.event_type,
                schema_version=event.schema_version,
                payload=redact(event.payload),
                delivery_status=outbox.status if outbox else None,
                publish_attempt_count=(
                    outbox.attempt_count if outbox else None
                ),
                created_by_type=event.created_by_type,
                created_by=event.created_by,
                updated_by_type=event.updated_by_type,
                updated_by=event.updated_by,
                available_at=outbox.available_at if outbox else None,
                created_at=event.created_at,
                updated_at=event.updated_at,
                published_at=outbox.published_at if outbox else None,
                last_error=outbox.last_error if outbox else None,
            )
            for event, outbox in page_rows
        ]
        next_cursor = (
            encode_integer_cursor(
                "execution_events",
                page_rows[-1][0].event_sequence,
            )
            if len(rows) > limit and page_rows
            else None
        )
        return Page(items=items, next_cursor=next_cursor)
