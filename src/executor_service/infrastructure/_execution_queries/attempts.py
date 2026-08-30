"""SQLAlchemy reads for Execution Attempts and Step Attempts."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.execution_queries import (
    ExecutionAttemptView,
    ExecutionStepAttemptView,
)
from executor_service.application.pagination import (
    Page,
    decode_integer_cursor,
    encode_integer_cursor,
)
from executor_service.domain.errors import ExecutionAttemptNotFoundError
from executor_service.infrastructure._execution_queries.guards import (
    require_attempt,
    require_execution,
)
from executor_service.infrastructure._execution_queries.mappers import (
    attempt_view,
    step_attempt_view,
)
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionStepAttemptORM,
)


class SQLAlchemyAttemptReader:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def attempts(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionAttemptView]:
        async with self._session_factory() as session:
            await require_execution(session, execution_id)
            statement = select(ExecutionAttemptORM).where(
                ExecutionAttemptORM.execution_id == execution_id
            )
            if cursor is not None:
                attempt_number = decode_integer_cursor(
                    cursor, "execution_attempts"
                )
                statement = statement.where(
                    ExecutionAttemptORM.attempt_number > attempt_number
                )
            attempts = list(
                await session.scalars(
                    statement.order_by(
                        ExecutionAttemptORM.attempt_number
                    ).limit(limit + 1)
                )
            )
            page_attempts = attempts[:limit]
            step_counts = await _step_counts(
                session, [row.id for row in page_attempts]
            )
        items = [
            attempt_view(row, step_counts.get(row.id, 0))
            for row in page_attempts
        ]
        next_cursor = (
            encode_integer_cursor(
                "execution_attempts", page_attempts[-1].attempt_number
            )
            if len(attempts) > limit and page_attempts
            else None
        )
        return Page(items=items, next_cursor=next_cursor)

    async def attempt(
        self, execution_id: UUID, attempt_id: UUID
    ) -> ExecutionAttemptView:
        async with self._session_factory() as session:
            await require_execution(session, execution_id)
            row = await session.scalar(
                select(ExecutionAttemptORM).where(
                    ExecutionAttemptORM.id == attempt_id,
                    ExecutionAttemptORM.execution_id == execution_id,
                )
            )
            if row is None:
                raise ExecutionAttemptNotFoundError(
                    f"Execution Attempt {attempt_id} was not found in "
                    f"Execution {execution_id}."
                )
            step_count = await session.scalar(
                select(func.count(ExecutionStepAttemptORM.id)).where(
                    ExecutionStepAttemptORM.execution_attempt_id == attempt_id
                )
            )
        return attempt_view(row, step_count or 0)

    async def attempt_steps(
        self,
        execution_id: UUID,
        attempt_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionStepAttemptView]:
        async with self._session_factory() as session:
            await require_attempt(session, execution_id, attempt_id)
            statement = select(ExecutionStepAttemptORM).where(
                ExecutionStepAttemptORM.execution_attempt_id == attempt_id
            )
            if cursor is not None:
                sequence = decode_integer_cursor(
                    cursor, "execution_step_attempts"
                )
                statement = statement.where(
                    ExecutionStepAttemptORM.sequence > sequence
                )
            rows = list(
                await session.scalars(
                    statement.order_by(ExecutionStepAttemptORM.sequence).limit(
                        limit + 1
                    )
                )
            )
        page_rows = rows[:limit]
        next_cursor = (
            encode_integer_cursor(
                "execution_step_attempts", page_rows[-1].sequence
            )
            if len(rows) > limit and page_rows
            else None
        )
        return Page(
            items=[step_attempt_view(row) for row in page_rows],
            next_cursor=next_cursor,
        )


async def _step_counts(
    session: AsyncSession, attempt_ids: list[UUID]
) -> dict[UUID, int]:
    if not attempt_ids:
        return {}
    rows = await session.execute(
        select(
            ExecutionStepAttemptORM.execution_attempt_id,
            func.count(ExecutionStepAttemptORM.id),
        )
        .where(ExecutionStepAttemptORM.execution_attempt_id.in_(attempt_ids))
        .group_by(ExecutionStepAttemptORM.execution_attempt_id)
    )
    return {attempt_id: count for attempt_id, count in rows}
