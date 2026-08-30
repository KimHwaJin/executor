"""SQLAlchemy reads for Executions and their planned Steps."""

from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import load_only, noload

from executor_service.application.execution_queries import (
    ExecutionDetailView,
    ExecutionSummaryView,
)
from executor_service.application.pagination import (
    Page,
    decode_integer_cursor,
    decode_time_cursor,
    encode_integer_cursor,
    encode_time_cursor,
)
from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.errors import ExecutionNotFoundError
from executor_service.domain.models import ExecutionStep
from executor_service.infrastructure._execution_queries.guards import (
    require_execution,
)
from executor_service.infrastructure._execution_queries.mappers import (
    EXECUTION_DETAIL_COLUMNS,
    EXECUTION_SUMMARY_COLUMNS,
    execution_detail_view,
    execution_summary_view,
)
from executor_service.infrastructure.db.models import (
    ExecutionORM,
    ExecutionStepORM,
)


class SQLAlchemyExecutionReader:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def executions(
        self,
        *,
        user_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        workflow_id: str | None = None,
        status: ExecutionStatus | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionSummaryView]:
        step_count = (
            select(func.count(ExecutionStepORM.id))
            .where(ExecutionStepORM.execution_id == ExecutionORM.id)
            .correlate(ExecutionORM)
            .scalar_subquery()
        )
        statement = select(
            ExecutionORM, step_count.label("step_count")
        ).options(
            load_only(*EXECUTION_SUMMARY_COLUMNS),
            noload(ExecutionORM.steps),
        )
        if user_id is not None:
            statement = statement.where(ExecutionORM.user_id == user_id)
        if project_id is not None:
            statement = statement.where(ExecutionORM.project_id == project_id)
        if session_id is not None:
            statement = statement.where(ExecutionORM.session_id == session_id)
        if task_id is not None:
            statement = statement.where(ExecutionORM.task_id == task_id)
        if workflow_id is not None:
            statement = statement.where(
                ExecutionORM.workflow_id == workflow_id
            )
        if status is not None:
            statement = statement.where(ExecutionORM.status == status)
        if cursor is not None:
            created_at, item_id = decode_time_cursor(cursor, "executions")
            statement = statement.where(
                or_(
                    ExecutionORM.created_at < created_at,
                    and_(
                        ExecutionORM.created_at == created_at,
                        ExecutionORM.id < item_id,
                    ),
                )
            )
        statement = statement.order_by(
            ExecutionORM.created_at.desc(), ExecutionORM.id.desc()
        ).limit(limit + 1)
        async with self._session_factory() as session:
            rows = list((await session.execute(statement)).all())
        page_rows = rows[:limit]
        next_cursor = (
            encode_time_cursor(
                "executions", page_rows[-1][0].created_at, page_rows[-1][0].id
            )
            if len(rows) > limit and page_rows
            else None
        )
        return Page(
            items=[
                execution_summary_view(row, count) for row, count in page_rows
            ],
            next_cursor=next_cursor,
        )

    async def execution(self, execution_id: UUID) -> ExecutionDetailView:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ExecutionORM)
                .where(ExecutionORM.id == execution_id)
                .options(
                    load_only(*EXECUTION_DETAIL_COLUMNS),
                    noload(ExecutionORM.steps),
                )
            )
        if row is None:
            raise ExecutionNotFoundError(
                f"Execution {execution_id} was not found."
            )
        return execution_detail_view(row)

    async def steps(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionStep]:
        async with self._session_factory() as session:
            await require_execution(session, execution_id)
            statement = select(ExecutionStepORM).where(
                ExecutionStepORM.execution_id == execution_id
            )
            if cursor is not None:
                sequence = decode_integer_cursor(cursor, "execution_steps")
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
            encode_integer_cursor("execution_steps", page_rows[-1].sequence)
            if len(rows) > limit and page_rows
            else None
        )
        return Page(
            items=[row.to_domain() for row in page_rows],
            next_cursor=next_cursor,
        )

    async def step(self, execution_id: UUID, step_id: UUID) -> ExecutionStep:
        async with self._session_factory() as session:
            await require_execution(session, execution_id)
            row = await session.scalar(
                select(ExecutionStepORM).where(
                    ExecutionStepORM.id == step_id,
                    ExecutionStepORM.execution_id == execution_id,
                )
            )
        if row is None:
            raise ExecutionNotFoundError(
                f"Execution Step {step_id} was not found in "
                f"Execution {execution_id}."
            )
        return row.to_domain()
