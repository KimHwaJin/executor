"""Hand a running Step to another Worker without touching its kernel.

The Jupyter extension records every Step in a journal. A Worker that stops
(scale-in, rolling update) or stops responding (crash, OOM) releases the
Execution lease while the Step keeps running. The next Worker to claim it
replays the journal from the start into its own result fence.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.domain.enums import HandoverReason, StepStatus
from executor_service.domain.runtime import (
    RuntimeFileState,
    RuntimeStorageSnapshot,
)
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionHandoverORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    ExecutionStepRunORM,
)
from executor_service.infrastructure.execution_worker.event_writer.payloads import (
    attempt_payload,
    operation_payload,
)
from executor_service.infrastructure.execution_worker.event_writer.persistence import (
    persist_execution_event,
)


@dataclass(frozen=True, slots=True)
class StepHandover:
    reason: HandoverReason
    paused_since: datetime


@dataclass(frozen=True, slots=True)
class StepRun:
    """One Step's Jupyter journal request, as recorded at Step start."""

    sequence: int
    request_id: UUID
    submitted: bool
    artifact_baseline: RuntimeStorageSnapshot


def snapshot_to_json(snapshot: RuntimeStorageSnapshot) -> dict[str, Any]:
    return asdict(snapshot)


def snapshot_from_json(value: dict[str, Any]) -> RuntimeStorageSnapshot:
    return RuntimeStorageSnapshot(
        files=tuple(RuntimeFileState(**item) for item in value["files"]),
        manifest_size=int(value["manifest_size"]),
    )


async def running_step_attempt(
    session: AsyncSession, execution_id: UUID
) -> ExecutionStepAttemptORM | None:
    """The running Step attempt whose code went to a Jupyter journal."""

    return await session.scalar(
        select(ExecutionStepAttemptORM)
        .join(
            ExecutionStepRunORM,
            ExecutionStepRunORM.execution_step_attempt_id
            == ExecutionStepAttemptORM.id,
        )
        .where(
            ExecutionStepAttemptORM.execution_id == execution_id,
            ExecutionStepAttemptORM.status == StepStatus.RUNNING,
        )
    )


async def begin_handover(
    session: AsyncSession,
    execution: ExecutionORM,
    attempt: ExecutionAttemptORM,
    step_attempt: ExecutionStepAttemptORM,
    reason: HandoverReason,
    *,
    now: datetime,
    paused_since: datetime,
) -> None:
    """Release the lease of a locked RUNNING Execution for another Worker."""

    execution.lease_owner = None
    execution.lease_expires_at = None
    execution.heartbeat_at = None
    # Fence the previous owner out even if it is only partitioned.
    execution.fencing_token += 1
    execution.updated_at = now
    execution.version += 1
    attempt.lease_owner = None
    attempt.lease_expires_at = None
    session.add(
        ExecutionHandoverORM(
            execution_id=execution.id,
            reason=reason,
            paused_since=paused_since,
        )
    )
    step = await session.get(ExecutionStepORM, step_attempt.execution_step_id)
    if step is None:
        raise ValueError("Running Step was not found for handover.")
    await persist_execution_event(
        session,
        execution.id,
        "execution.step_handover_started",
        {
            "status": "RUNNING",
            "operation": await operation_payload(session, step.operation_id),
            "step": {"id": str(step.id), "sequence": step.sequence},
            "attempt": await attempt_payload(session, attempt.id),
            "reason": reason.value,
            "kernel_running": True,
            "detected_at": now,
        },
    )


async def add_handover_completed_event(
    session: AsyncSession,
    execution_id: UUID,
    attempt_id: UUID,
    step: ExecutionStepORM,
    *,
    paused_seconds: float,
    finished_during_handover: bool,
) -> None:
    await persist_execution_event(
        session,
        execution_id,
        "execution.step_handover_completed",
        {
            "status": "RUNNING",
            "operation": await operation_payload(session, step.operation_id),
            "step": {"id": str(step.id), "sequence": step.sequence},
            "attempt": await attempt_payload(session, attempt_id),
            "paused_seconds": round(max(paused_seconds, 0.0), 3),
            "outputs_complete": True,
            "finished_during_handover": finished_during_handover,
        },
    )
