"""SQLAlchemy reads for Execution Operations and their Steps."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.execution_queries import (
    ExecutionOperationView,
)
from executor_service.application.pagination import (
    Page,
    decode_integer_cursor,
    encode_integer_cursor,
)
from executor_service.domain.errors import ExecutionOperationNotFoundError
from executor_service.domain.models import ExecutionStep
from executor_service.infrastructure._execution_queries.guards import (
    require_execution,
    require_operation,
)
from executor_service.infrastructure._execution_queries.mappers import (
    operation_view,
)
from executor_service.infrastructure.db.models import (
    ExecutionOperationORM,
    ExecutionStepORM,
)


class SQLAlchemyOperationReader:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def operations(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionOperationView]:
        async with self._session_factory() as session:
            await require_execution(session, execution_id)
            statement = select(ExecutionOperationORM).where(
                ExecutionOperationORM.execution_id == execution_id
            )
            if cursor is not None:
                operation_number = decode_integer_cursor(
                    cursor, "execution_operations"
                )
                statement = statement.where(
                    ExecutionOperationORM.operation_number > operation_number
                )
            rows = list(
                await session.scalars(
                    statement.order_by(
                        ExecutionOperationORM.operation_number
                    ).limit(limit + 1)
                )
            )
        page_rows = rows[:limit]
        next_cursor = (
            encode_integer_cursor(
                "execution_operations", page_rows[-1].operation_number
            )
            if len(rows) > limit and page_rows
            else None
        )
        return Page(
            items=[operation_view(row) for row in page_rows],
            next_cursor=next_cursor,
        )

    async def operation(
        self, execution_id: UUID, operation_id: UUID
    ) -> ExecutionOperationView:
        async with self._session_factory() as session:
            await require_execution(session, execution_id)
            row = await session.scalar(
                select(ExecutionOperationORM).where(
                    ExecutionOperationORM.id == operation_id,
                    ExecutionOperationORM.execution_id == execution_id,
                )
            )
        if row is None:
            raise ExecutionOperationNotFoundError(
                f"Execution Operation {operation_id} was not found in "
                f"Execution {execution_id}."
            )
        return operation_view(row)

    async def operation_steps(
        self,
        execution_id: UUID,
        operation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionStep]:
        async with self._session_factory() as session:
            await require_operation(session, execution_id, operation_id)
            statement = select(ExecutionStepORM).where(
                ExecutionStepORM.execution_id == execution_id,
                ExecutionStepORM.operation_id == operation_id,
            )
            if cursor is not None:
                sequence = decode_integer_cursor(
                    cursor, "execution_operation_steps"
                )
                statement = statement.where(
                    ExecutionStepORM.sequence > sequence
                )
            rows = list(
                await session.scalars(
                    statement.order_by(ExecutionStepORM.sequence).limit(
                        limit + 1
                    )
                )
            )
        page_rows = rows[:limit]
        next_cursor = (
            encode_integer_cursor(
                "execution_operation_steps", page_rows[-1].sequence
            )
            if len(rows) > limit and page_rows
            else None
        )
        return Page(
            items=[row.to_domain() for row in page_rows],
            next_cursor=next_cursor,
        )
