"""Why each Execution is in its status.

Every input is a PostgreSQL row, so each Executor replica gives the same
answer. A Worker's free slots live only in its own process: an Execution
that a Runtime Target could take but no Worker has started reports
WAITING_FOR_WORKER with its place in the queue.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.application.execution_queries import StatusReason
from executor_service.domain.enums import (
    ExecutionStatus,
    FailureType,
    FinalizedBy,
    RuntimeTargetStatus,
    StatusReasonCode,
)
from executor_service.infrastructure.db.models import (
    ExecutionHandoverORM,
    ExecutionORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.maintenance import (
    ExecutorMaintenanceService,
)
from executor_service.infrastructure.runtime_target_selector import (
    RuntimeTargetSelector,
)
from executor_service.settings import Settings

_MESSAGES: dict[StatusReasonCode | FinalizedBy, str] = {
    StatusReasonCode.ADMISSION_DRAINING: (
        "The Executor is draining for maintenance and starts no new "
        "Executions."
    ),
    StatusReasonCode.NO_ACTIVE_TARGET: (
        "No enabled, ACTIVE Runtime Target serves this pool."
    ),
    StatusReasonCode.PROFILE_UNSUPPORTED: (
        "No ACTIVE Runtime Target supports this profile."
    ),
    StatusReasonCode.CAPACITY_FULL: (
        "Every matching Runtime Target is at its concurrent Execution limit."
    ),
    StatusReasonCode.MEMORY_LIMIT: (
        "Every matching Runtime Target is above the memory admission limit."
    ),
    StatusReasonCode.TARGET_UNAVAILABLE: (
        "The Runtime Target holding this Execution's session is offline or "
        "disabled."
    ),
    StatusReasonCode.WAITING_FOR_WORKER: (
        "A Runtime Target can take the Execution; it waits for a free "
        "Worker slot."
    ),
    StatusReasonCode.WORKER_HANDOVER: (
        "The Worker running this Execution stopped; another Worker is "
        "taking it over."
    ),
    StatusReasonCode.AWAITING_NEXT_OPERATION: (
        "Waiting for the next Operation or a finalize call."
    ),
    StatusReasonCode.CANCELLED_BY_REQUEST: "Cancellation was requested.",
    StatusReasonCode.COMPLETED: "Every Step completed.",
    FinalizedBy.AGENT: "The agent finalized the Execution.",
    FinalizedBy.WAIT_TIMEOUT: (
        "No next Operation arrived before the wait deadline."
    ),
    FinalizedBy.MAX_RUNTIME: "The Execution reached its maximum runtime.",
}

# The ORM columns a reason is read from.
STATUS_REASON_COLUMNS = (
    ExecutionORM.runtime_type,
    ExecutionORM.runtime_pool,
    ExecutionORM.runtime_profile,
    ExecutionORM.runtime_target_id,
    ExecutionORM.runtime_session_id,
    ExecutionORM.cancellation_reason,
    ExecutionORM.finalized_by,
    ExecutionORM.failure_type,
    ExecutionORM.error_message,
)


class StatusReasonResolver:
    def __init__(self, settings: Settings) -> None:
        self._selector = RuntimeTargetSelector(settings)

    async def resolve(
        self, session: AsyncSession, rows: Sequence[ExecutionORM]
    ) -> dict[UUID, StatusReason | None]:
        waits = await self._waits(
            session,
            [row for row in rows if row.status == ExecutionStatus.QUEUED],
        )
        handovers = await self._handovers(
            session,
            [row.id for row in rows if row.status == ExecutionStatus.RUNNING],
        )
        return {
            row.id: waits.get(row.id)
            or handovers.get(row.id)
            or _stored_reason(row)
            for row in rows
        }

    async def _waits(
        self, session: AsyncSession, queued: list[ExecutionORM]
    ) -> dict[UUID, StatusReason]:
        """Mirror the Worker's claim: what stops it starting each run."""
        if not queued:
            return {}
        positions = await self._queue_positions(
            session, [row.id for row in queued]
        )
        # A run that kept its kernel returns to that Target and bypasses
        # admission; any other run takes whichever Target admits it.
        pinned = {
            row.id: row.runtime_target_id
            for row in queued
            if row.runtime_session_id is not None
            and row.runtime_target_id is not None
        }
        targets = (
            {
                target.id: target
                for target in await session.scalars(
                    select(RuntimeTargetORM).where(
                        RuntimeTargetORM.id.in_(set(pinned.values()))
                    )
                )
            }
            if pinned
            else {}
        )
        draining = len(pinned) < len(queued) and not (
            await ExecutorMaintenanceService.admission_is_active(session)
        )
        blocks: dict[
            tuple[str, str, str], tuple[StatusReasonCode, dict[str, str]]
        ] = {}
        reasons: dict[UUID, StatusReason] = {}
        for row in queued:
            if row.id in pinned:
                target = targets.get(pinned[row.id])
                state = _unavailable(target)
                code, values = (
                    (StatusReasonCode.WAITING_FOR_WORKER, {})
                    if state is None
                    else (
                        StatusReasonCode.TARGET_UNAVAILABLE,
                        {
                            target.name
                            if target
                            else str(pinned[row.id]): state
                        },
                    )
                )
            elif draining:
                code, values = StatusReasonCode.ADMISSION_DRAINING, {}
            else:
                key = (
                    row.runtime_pool.value,
                    row.runtime_type.value,
                    row.runtime_profile,
                )
                if key not in blocks:
                    _target, blocked, values = await self._selector.verdict(
                        session, row
                    )
                    blocks[key] = (
                        blocked or StatusReasonCode.WAITING_FOR_WORKER,
                        values,
                    )
                code, values = blocks[key]
            detail: dict[str, Any] = {"queue_position": positions[row.id]}
            if values:
                detail["targets"] = values
            reasons[row.id] = StatusReason(code, _MESSAGES[code], detail)
        return reasons

    @staticmethod
    async def _queue_positions(
        session: AsyncSession, execution_ids: list[UUID]
    ) -> dict[UUID, int]:
        """Each run's place in the order Workers take waiting runs."""
        ranked = (
            select(
                ExecutionORM.id,
                func.row_number()
                .over(order_by=(ExecutionORM.created_at, ExecutionORM.id))
                .label("position"),
            )
            .where(
                ExecutionORM.status.in_(
                    [ExecutionStatus.QUEUED, ExecutionStatus.FINALIZING]
                )
            )
            .subquery()
        )
        rows = await session.execute(
            select(ranked.c.id, ranked.c.position).where(
                ranked.c.id.in_(execution_ids)
            )
        )
        return {execution_id: position for execution_id, position in rows}

    @staticmethod
    async def _handovers(
        session: AsyncSession, execution_ids: list[UUID]
    ) -> dict[UUID, StatusReason]:
        if not execution_ids:
            return {}
        return {
            handover.execution_id: StatusReason(
                StatusReasonCode.WORKER_HANDOVER,
                _MESSAGES[StatusReasonCode.WORKER_HANDOVER],
                {
                    "handover_reason": handover.reason.value,
                    "paused_since": handover.paused_since,
                },
            )
            for handover in await session.scalars(
                select(ExecutionHandoverORM).where(
                    ExecutionHandoverORM.execution_id.in_(execution_ids)
                )
            )
        }


def _unavailable(target: RuntimeTargetORM | None) -> str | None:
    if target is None:
        return "MISSING"
    if not target.enabled:
        return "DISABLED"
    if target.status == RuntimeTargetStatus.OFFLINE:
        return RuntimeTargetStatus.OFFLINE.value
    return None


def _stored_reason(row: ExecutionORM) -> StatusReason | None:
    """Reasons the Execution row itself records."""
    code: StatusReasonCode | FinalizedBy
    match row.status:
        case ExecutionStatus.FAILED:
            return StatusReason(
                row.failure_type or FailureType.INTERNAL_ERROR,
                row.error_message or "The Execution failed.",
                {"finalized_by": row.finalized_by.value}
                if row.finalized_by
                else None,
            )
        case ExecutionStatus.CANCEL_REQUESTED | ExecutionStatus.CANCELLED:
            code = (
                FinalizedBy.MAX_RUNTIME
                if row.finalized_by == FinalizedBy.MAX_RUNTIME
                else StatusReasonCode.CANCELLED_BY_REQUEST
            )
            return StatusReason(
                code, row.cancellation_reason or _MESSAGES[code]
            )
        case ExecutionStatus.FINALIZING | ExecutionStatus.SUCCEEDED:
            code = row.finalized_by or StatusReasonCode.COMPLETED
        case ExecutionStatus.WAITING_FOR_OPERATION:
            code = StatusReasonCode.AWAITING_NEXT_OPERATION
        case _:
            return None
    return StatusReason(code, _MESSAGES[code])
