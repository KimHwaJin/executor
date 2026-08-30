"""Shared existence checks for execution query readers."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.domain.errors import (
    ExecutionAttemptNotFoundError,
    ExecutionNotFoundError,
    ExecutionOperationNotFoundError,
)
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionORM,
)


async def require_execution(session: AsyncSession, execution_id: UUID) -> None:
    exists = await session.scalar(
        select(ExecutionORM.id).where(ExecutionORM.id == execution_id)
    )
    if exists is None:
        raise ExecutionNotFoundError(
            f"Execution {execution_id} was not found."
        )


async def require_attempt(
    session: AsyncSession, execution_id: UUID, attempt_id: UUID
) -> None:
    await require_execution(session, execution_id)
    exists = await session.scalar(
        select(ExecutionAttemptORM.id).where(
            ExecutionAttemptORM.id == attempt_id,
            ExecutionAttemptORM.execution_id == execution_id,
        )
    )
    if exists is None:
        raise ExecutionAttemptNotFoundError(
            f"Execution Attempt {attempt_id} was not found in "
            f"Execution {execution_id}."
        )


async def require_operation(
    session: AsyncSession, execution_id: UUID, operation_id: UUID
) -> None:
    await require_execution(session, execution_id)
    exists = await session.scalar(
        select(ExecutionOperationORM.id).where(
            ExecutionOperationORM.id == operation_id,
            ExecutionOperationORM.execution_id == execution_id,
        )
    )
    if exists is None:
        raise ExecutionOperationNotFoundError(
            f"Execution Operation {operation_id} was not found in "
            f"Execution {execution_id}."
        )
