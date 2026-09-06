"""Remove active registration, retaining immutable historical identity."""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from executor_service.application.runtime_targets import (
    PurgeRuntimeTargetCommand,
    RuntimeTargetPurgeView,
)
from executor_service.domain.enums import (
    ExecutionStatus,
    RuntimeSessionCleanupStatus,
    RuntimeTargetStatus,
)
from executor_service.domain.errors import (
    RuntimeTargetNotFoundError,
    RuntimeTargetPurgeConflictError,
)
from executor_service.domain.models import utc_now
from executor_service.infrastructure._runtime_registry.mappers import (
    purge_view,
)
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionORM,
    RuntimeTargetORM,
    RuntimeTargetPurgeORM,
)
from executor_service.infrastructure.runtime_admission import (
    count_runtime_reservations,
)


async def purge_target(
    session: AsyncSession, command: PurgeRuntimeTargetCommand
) -> RuntimeTargetPurgeView:
    # Scheduling, activation and identity updates lock this same row. Do not
    # lock Execution rows here: Worker lock order is Execution -> Target.
    target = await session.scalar(
        select(RuntimeTargetORM)
        .where(RuntimeTargetORM.id == command.target_id)
        .with_for_update()
    )
    if target is None:
        # Read AFTER the lock attempt so concurrent deletes see the winner's
        # committed tombstone under PostgreSQL READ COMMITTED isolation.
        tombstone = await session.scalar(
            select(RuntimeTargetPurgeORM).where(
                RuntimeTargetPurgeORM.target_id == command.target_id
            )
        )
        if tombstone is not None:
            return purge_view(tombstone)
        raise RuntimeTargetNotFoundError(
            f"Runtime Target {command.target_id} was not found."
        )
    if target.enabled or target.status != RuntimeTargetStatus.OFFLINE:
        raise RuntimeTargetPurgeConflictError(
            "Disable the Runtime Target before purging it."
        )
    if await count_runtime_reservations(session, target.id, utc_now()):
        raise RuntimeTargetPurgeConflictError(
            "Runtime Target has active, retained or cleanup reservations."
        )
    # An expired retention window alone does not prove kernel deletion. Keep
    # credentials available until actual cleanup succeeds, also for Attempts
    # no longer referenced by the latest Execution.runtime_target_id.
    pending_execution = await session.scalar(
        select(ExecutionORM.id)
        .where(
            ExecutionORM.runtime_target_id == target.id,
            or_(
                ExecutionORM.status.not_in(
                    [
                        ExecutionStatus.SUCCEEDED,
                        ExecutionStatus.FAILED,
                        ExecutionStatus.CANCELLED,
                    ]
                ),
                ExecutionORM.runtime_session_id.is_not(None)
                & (
                    ExecutionORM.runtime_session_cleanup_status
                    != RuntimeSessionCleanupStatus.SUCCEEDED
                ),
            ),
        )
        .limit(1)
    )
    completed_attempt = aliased(ExecutionAttemptORM)
    session_was_cleaned = (
        select(completed_attempt.id)
        .where(
            completed_attempt.execution_id == ExecutionAttemptORM.execution_id,
            completed_attempt.runtime_target_id
            == ExecutionAttemptORM.runtime_target_id,
            completed_attempt.runtime_session_id
            == ExecutionAttemptORM.runtime_session_id,
            completed_attempt.attempt_number
            >= ExecutionAttemptORM.attempt_number,
            completed_attempt.runtime_session_cleanup_status
            == RuntimeSessionCleanupStatus.SUCCEEDED,
        )
        .exists()
    )
    execution_was_cleaned = (
        select(ExecutionORM.id)
        .where(
            ExecutionORM.id == ExecutionAttemptORM.execution_id,
            ExecutionORM.runtime_target_id
            == ExecutionAttemptORM.runtime_target_id,
            ExecutionORM.runtime_session_id.is_(None),
            ExecutionORM.runtime_session_cleanup_status
            == RuntimeSessionCleanupStatus.SUCCEEDED,
            # Only use the Execution's latest Attempt as proof; a later,
            # different kernel being cleaned cannot prove this one was.
            ~select(completed_attempt.id)
            .where(
                completed_attempt.execution_id
                == ExecutionAttemptORM.execution_id,
                completed_attempt.attempt_number
                > ExecutionAttemptORM.attempt_number,
            )
            .exists(),
        )
        .correlate(ExecutionAttemptORM)
        .exists()
    )
    pending_attempt = await session.scalar(
        select(ExecutionAttemptORM.id)
        .where(
            ExecutionAttemptORM.runtime_target_id == target.id,
            ExecutionAttemptORM.runtime_session_id.is_not(None),
            ExecutionAttemptORM.runtime_session_cleanup_status
            != RuntimeSessionCleanupStatus.SUCCEEDED,
            ~session_was_cleaned,
            ~execution_was_cleaned,
        )
        .limit(1)
    )
    if pending_execution is not None or pending_attempt is not None:
        raise RuntimeTargetPurgeConflictError(
            "Runtime Target still has unfinished work or unconfirmed "
            "Runtime session cleanup."
        )
    tombstone = RuntimeTargetPurgeORM(
        target_id=target.id,
        target_name=target.name,
        runtime_type=target.runtime_type,
        connection_config=target.connection_config,
        pool=target.pool,
        created_by_type=command.actor_type,
        created_by=command.actor_id,
        updated_by_type=command.actor_type,
        updated_by=command.actor_id,
    )
    session.add(tombstone)
    await session.flush()
    await session.delete(target)
    await session.flush()
    return purge_view(tombstone)
