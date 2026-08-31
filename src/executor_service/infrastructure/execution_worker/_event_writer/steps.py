"""Step lifecycle event builders."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.domain.enums import StepStatus
from executor_service.domain.results import StepResultDescriptor
from executor_service.infrastructure.db.models import (
    ExecutionStepAttemptORM,
    ExecutionStepORM,
)
from executor_service.infrastructure.execution_leases import ExecutionLease
from executor_service.infrastructure.execution_worker._event_writer.payloads import (
    attempt_payload,
    event_output_summary,
    operation_payload,
    row_result_reference,
    stored_result_reference,
)
from executor_service.infrastructure.execution_worker._event_writer.persistence import (
    persist_execution_event,
)


async def add_step_started_event(
    session: AsyncSession,
    lease: ExecutionLease,
    step: ExecutionStepORM,
) -> None:
    await persist_execution_event(
        session,
        lease.execution_id,
        "execution.step_started",
        {
            "status": "RUNNING",
            "operation": await operation_payload(session, step.operation_id),
            "step": {"id": str(step.id), "sequence": step.sequence},
            "attempt": await attempt_payload(session, lease.attempt_id),
        },
    )


async def add_step_completed_event(
    session: AsyncSession,
    lease: ExecutionLease,
    step: ExecutionStepORM,
    status: StepStatus,
    *,
    stored_result: StepResultDescriptor | None,
    error_message: str | None = None,
    retryable: bool = False,
) -> None:
    result_ref = (
        stored_result_reference(stored_result)
        if stored_result is not None
        else None
    )
    output_summary = (
        event_output_summary(stored_result)
        if stored_result is not None
        else None
    )
    error = None
    if status != StepStatus.SUCCEEDED:
        error = {
            "code": (
                "STEP_CANCELLED"
                if status == StepStatus.CANCELLED
                else "STEP_EXECUTION_FAILED"
            ),
            "message": error_message or f"Step was {status.value.lower()}.",
            "retryable": retryable,
        }
    await persist_execution_event(
        session,
        lease.execution_id,
        "execution.step_completed",
        {
            "status": status.value,
            "operation": await operation_payload(session, step.operation_id),
            "step": {"id": str(step.id), "sequence": step.sequence},
            "attempt": await attempt_payload(session, lease.attempt_id),
            "result_ref": result_ref,
            "output_summary": output_summary,
            "error": error,
        },
    )


async def add_step_history_completed_event(
    session: AsyncSession,
    execution_id: UUID,
    step_id: UUID,
    attempt_id: UUID,
    status: StepStatus,
    *,
    error_message: str,
    retryable: bool,
) -> None:
    step = await session.get(ExecutionStepORM, step_id)
    history = await session.scalar(
        select(ExecutionStepAttemptORM).where(
            ExecutionStepAttemptORM.execution_step_id == step_id,
            ExecutionStepAttemptORM.execution_attempt_id == attempt_id,
        )
    )
    if step is None or history is None:
        return
    result_ref = row_result_reference(history)
    output_summary = None
    if result_ref is not None:
        raw_mime_types = history.output_summary.get("mime_types", [])
        output_summary = {
            "count": int(history.output_summary.get("output_count", 0)),
            "content_types": (
                sorted(set(raw_mime_types))
                if isinstance(raw_mime_types, list)
                else []
            ),
        }
    await persist_execution_event(
        session,
        execution_id,
        "execution.step_completed",
        {
            "status": status.value,
            "operation": await operation_payload(session, step.operation_id),
            "step": {"id": str(step.id), "sequence": step.sequence},
            "attempt": await attempt_payload(session, attempt_id),
            "result_ref": result_ref,
            "output_summary": output_summary,
            "error": {
                "code": (
                    "STEP_CANCELLED"
                    if status == StepStatus.CANCELLED
                    else "STEP_EXECUTION_FAILED"
                ),
                "message": error_message,
                "retryable": retryable,
            },
        },
    )
