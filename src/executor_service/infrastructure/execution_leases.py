"""Database-backed Worker lease fencing primitives."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

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


@dataclass(frozen=True, slots=True)
class CancellationLease:
    execution_id: UUID
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
    # One statement locks the Execution row and then its Attempt row;
    # the check runs several times per Step.
    row = (
        await session.execute(
            select(ExecutionORM, ExecutionAttemptORM)
            .join(
                ExecutionAttemptORM,
                ExecutionAttemptORM.execution_id == ExecutionORM.id,
            )
            .where(
                ExecutionORM.id == lease.execution_id,
                ExecutionORM.status.in_(allowed_statuses),
                ExecutionORM.lease_owner == lease.owner,
                ExecutionORM.fencing_token == lease.fencing_token,
                ExecutionORM.lease_expires_at.is_not(None),
                ExecutionORM.lease_expires_at > now,
                ExecutionAttemptORM.id == lease.attempt_id,
                ExecutionAttemptORM.status == AttemptStatus.RUNNING,
                ExecutionAttemptORM.lease_owner == lease.owner,
                ExecutionAttemptORM.fencing_token == lease.fencing_token,
                ExecutionAttemptORM.lease_expires_at.is_not(None),
                ExecutionAttemptORM.lease_expires_at > now,
            )
            # Loading every Step of the Execution with it would grow
            # with the session's history.
            .options(lazyload(ExecutionORM.steps))
            .with_for_update()
        )
    ).first()
    if row is None:
        raise ExecutionLeaseLostError(
            f"Execution {lease.execution_id} Attempt {lease.attempt_id} "
            f"lease is no longer owned by {lease.owner} at fence "
            f"{lease.fencing_token}."
        )
    execution, attempt = row
    # The session only holds unchanged rows weakly. A caller that drops
    # the result would make every later ``session.get`` of these rows in
    # the same transaction (event payloads, actors) query them again.
    session.info["active_lease_rows"] = (execution, attempt)
    return execution, attempt


async def require_active_cancellation_lease(
    session: AsyncSession,
    lease: CancellationLease,
) -> ExecutionORM:
    """Lock the Execution only while the exact cancellation lease is active."""

    now = utc_now()
    execution = await session.scalar(
        select(ExecutionORM)
        .where(
            ExecutionORM.id == lease.execution_id,
            ExecutionORM.status == ExecutionStatus.CANCEL_REQUESTED,
            ExecutionORM.cancellation_lease_owner == lease.owner,
            ExecutionORM.fencing_token == lease.fencing_token,
            ExecutionORM.cancellation_lease_expires_at.is_not(None),
            ExecutionORM.cancellation_lease_expires_at > now,
        )
        .with_for_update()
    )
    if execution is None:
        raise ExecutionLeaseLostError(
            f"Execution {lease.execution_id} cancellation lease is no "
            f"longer owned by {lease.owner} at fence "
            f"{lease.fencing_token}."
        )
    return execution
