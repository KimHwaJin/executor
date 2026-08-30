"""Bulk SQLAlchemy reads used to assemble result snapshots."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import load_only, noload

from executor_service.application.execution_queries import (
    ExecutionResultSnapshot,
    OperationResultSnapshot,
)
from executor_service.domain.errors import (
    ExecutionNotFoundError,
    ExecutionOperationNotFoundError,
)
from executor_service.infrastructure._execution_queries.mappers import (
    EXECUTION_DETAIL_COLUMNS,
    artifact_view,
    attempt_view,
    execution_detail_view,
    operation_view,
)
from executor_service.infrastructure.db.models import (
    ExecutionArtifactORM,
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
)


class SQLAlchemyResultSnapshotReader:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._session_factory = session_factory

    async def operation_result_snapshot(
        self, execution_id: UUID, operation_id: UUID
    ) -> OperationResultSnapshot:
        async with self._session_factory() as session:
            execution = await _execution_row(session, execution_id)
            operation = await session.scalar(
                select(ExecutionOperationORM).where(
                    ExecutionOperationORM.id == operation_id,
                    ExecutionOperationORM.execution_id == execution_id,
                )
            )
            if operation is None:
                raise ExecutionOperationNotFoundError(
                    f"Execution Operation {operation_id} was not found in "
                    f"Execution {execution_id}."
                )
            steps = tuple(
                row.to_domain()
                for row in await session.scalars(
                    select(ExecutionStepORM)
                    .where(
                        ExecutionStepORM.execution_id == execution_id,
                        ExecutionStepORM.operation_id == operation_id,
                    )
                    .order_by(ExecutionStepORM.sequence)
                )
            )
        return OperationResultSnapshot(
            execution=execution_detail_view(execution),
            operation=operation_view(operation),
            steps=steps,
        )

    async def execution_result_snapshot(
        self, execution_id: UUID
    ) -> ExecutionResultSnapshot:
        async with self._session_factory() as session:
            execution = await _execution_row(session, execution_id)
            operations = tuple(
                await session.scalars(
                    select(ExecutionOperationORM)
                    .where(ExecutionOperationORM.execution_id == execution_id)
                    .order_by(ExecutionOperationORM.operation_number)
                )
            )
            steps = tuple(
                row.to_domain()
                for row in await session.scalars(
                    select(ExecutionStepORM)
                    .where(ExecutionStepORM.execution_id == execution_id)
                    .order_by(ExecutionStepORM.sequence)
                )
            )
            attempt_rows = tuple(
                await session.scalars(
                    select(ExecutionAttemptORM)
                    .where(ExecutionAttemptORM.execution_id == execution_id)
                    .order_by(ExecutionAttemptORM.attempt_number)
                )
            )
            step_count_rows = (
                await session.execute(
                    select(
                        ExecutionStepAttemptORM.execution_attempt_id,
                        func.count(ExecutionStepAttemptORM.id),
                    )
                    .where(
                        ExecutionStepAttemptORM.execution_id == execution_id
                    )
                    .group_by(ExecutionStepAttemptORM.execution_attempt_id)
                )
            ).all()
            artifact_rows = tuple(
                await session.scalars(
                    select(ExecutionArtifactORM)
                    .where(ExecutionArtifactORM.execution_id == execution_id)
                    .order_by(
                        ExecutionArtifactORM.created_at,
                        ExecutionArtifactORM.id,
                    )
                )
            )
        step_counts = {
            attempt_id: count for attempt_id, count in step_count_rows
        }
        return ExecutionResultSnapshot(
            execution=execution_detail_view(execution),
            operations=tuple(operation_view(row) for row in operations),
            steps=steps,
            attempts=tuple(
                attempt_view(row, step_counts.get(row.id, 0))
                for row in attempt_rows
            ),
            artifacts=tuple(artifact_view(row) for row in artifact_rows),
        )


async def _execution_row(
    session: AsyncSession, execution_id: UUID
) -> ExecutionORM:
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
    return row
