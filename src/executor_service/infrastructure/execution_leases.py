"""Database-backed Worker lease fencing primitives."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.domain.enums import AttemptStatus, ExecutionStatus
from executor_service.domain.models import utc_now
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
)


class ExecutionLeaseLostError(RuntimeError):
    """The Worker no longer owns the authoritative Execution attempt."""


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    execution_id: UUID
    attempt_id: UUID
    owner: str
    fencing_token: int


async def require_active_lease(
    session: AsyncSession,
    lease: ExecutionLease,
    *,
    allowed_statuses: tuple[ExecutionStatus, ...] = (ExecutionStatus.RUNNING,),
) -> tuple[ExecutionORM, ExecutionAttemptORM]:
    """Lock and return rows only while the exact lease epoch is active."""

    now = utc_now()
    execution = await session.scalar(
        select(ExecutionORM)
        .where(
            ExecutionORM.id == lease.execution_id,
            ExecutionORM.status.in_(allowed_statuses),
            ExecutionORM.lease_owner == lease.owner,
            ExecutionORM.fencing_token == lease.fencing_token,
            ExecutionORM.lease_expires_at.is_not(None),
            ExecutionORM.lease_expires_at > now,
        )
        .with_for_update()
    )
    if execution is None:
        raise ExecutionLeaseLostError(
            f"Execution {lease.execution_id} lease is no longer owned by "
            f"{lease.owner} at fence {lease.fencing_token}."
        )
    attempt = await session.scalar(
        select(ExecutionAttemptORM)
        .where(
            ExecutionAttemptORM.id == lease.attempt_id,
            ExecutionAttemptORM.execution_id == lease.execution_id,
            ExecutionAttemptORM.status == AttemptStatus.RUNNING,
            ExecutionAttemptORM.lease_owner == lease.owner,
            ExecutionAttemptORM.fencing_token == lease.fencing_token,
            ExecutionAttemptORM.lease_expires_at.is_not(None),
            ExecutionAttemptORM.lease_expires_at > now,
        )
        .with_for_update()
    )
    if attempt is None:
        raise ExecutionLeaseLostError(
            f"Execution Attempt {lease.attempt_id} is no longer active at "
            f"fence {lease.fencing_token}."
        )
    return execution, attempt
