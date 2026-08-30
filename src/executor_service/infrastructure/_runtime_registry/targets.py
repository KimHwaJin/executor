"""Shared Runtime Target persistence lookups."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.domain.errors import RuntimeTargetNotFoundError
from executor_service.infrastructure.db.models import RuntimeTargetORM


async def required_target(
    session: AsyncSession,
    target_id: UUID,
    *,
    lock: bool = False,
) -> RuntimeTargetORM:
    statement = select(RuntimeTargetORM).where(
        RuntimeTargetORM.id == target_id
    )
    if lock:
        statement = statement.with_for_update()
    target = await session.scalar(statement)
    if target is None:
        raise RuntimeTargetNotFoundError(
            f"Runtime Target {target_id} was not found."
        )
    return target
