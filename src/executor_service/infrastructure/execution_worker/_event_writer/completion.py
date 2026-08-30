"""Operation and Execution completion event builders."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.domain.enums import (
    ExecutionStatus,
    OperationStatus,
    RetryStrategy,
    StepStatus,
)
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
)
from executor_service.infrastructure.execution_worker._event_writer.payloads import (
    row_result_reference,
)
from executor_service.infrastructure.execution_worker._event_writer.persistence import (
    persist_execution_event,
)


async def latest_step_attempts(
    session: AsyncSession, operation_id: UUID
) -> dict[UUID, tuple[ExecutionStepAttemptORM, ExecutionAttemptORM]]:
    rows = list(
        (
            await session.execute(
                select(ExecutionStepAttemptORM, ExecutionAttemptORM)
                .join(
                    ExecutionAttemptORM,
                    ExecutionAttemptORM.id
                    == ExecutionStepAttemptORM.execution_attempt_id,
                )
                .join(
                    ExecutionStepORM,
                    ExecutionStepORM.id
                    == ExecutionStepAttemptORM.execution_step_id,
                )
                .where(ExecutionStepORM.operation_id == operation_id)
                .order_by(ExecutionAttemptORM.attempt_number)
            )
        ).all()
    )
    latest: dict[
        UUID, tuple[ExecutionStepAttemptORM, ExecutionAttemptORM]
    ] = {}
    for history, attempt in rows:
        latest[history.execution_step_id] = (history, attempt)
    return latest


async def add_operation_completed_event(
    session: AsyncSession,
    execution_id: UUID,
    operation_id: UUID,
) -> None:
    execution = await session.get(ExecutionORM, execution_id)
    operation = await session.get(ExecutionOperationORM, operation_id)
    if execution is None or operation is None:
        return
    steps = list(
        await session.scalars(
            select(ExecutionStepORM)
            .where(ExecutionStepORM.operation_id == operation_id)
            .order_by(ExecutionStepORM.sequence)
        )
    )
    terminal = {
        StepStatus.SUCCEEDED,
        StepStatus.FAILED,
        StepStatus.CANCELLED,
    }
    latest = await latest_step_attempts(session, operation_id)
    step_results: list[dict[str, object]] = []
    for step in steps:
        attempt_pair = latest.get(step.id)
        if step.status not in terminal or attempt_pair is None:
            continue
        history, attempt = attempt_pair
        result_ref = row_result_reference(history)
        if step.status == StepStatus.SUCCEEDED and result_ref is None:
            continue
        step_results.append(
            {
                "step_id": str(step.id),
                "sequence": step.sequence,
                "status": step.status.value,
                "attempt": {
                    "id": str(attempt.id),
                    "number": attempt.attempt_number,
                    "reason": (
                        "INITIAL" if attempt.attempt_number == 1 else "RETRY"
                    ),
                },
                "result_ref": result_ref,
            }
        )
    counts = {
        status: sum(step.status == status for step in steps)
        for status in terminal
    }
    completed = sum(counts.values())
    continuation = None
    if (
        execution.status == ExecutionStatus.WAITING_FOR_OPERATION
        and execution.operation_wait_expires_at is not None
    ):
        continuation = {
            "allowed": True,
            "expected_version": execution.version,
            "expires_at": execution.operation_wait_expires_at,
        }
    error = None
    if operation.status != OperationStatus.SUCCEEDED:
        failed_step = next(
            (step for step in steps if step.status == StepStatus.FAILED),
            None,
        )
        error = {
            "code": (
                "OPERATION_CANCELLED"
                if operation.status == OperationStatus.CANCELLED
                else "OPERATION_STEP_FAILED"
            ),
            "message": operation.error_message
            or f"Operation was {operation.status.value.lower()}.",
            "step_id": str(failed_step.id) if failed_step else None,
            "retryable": (
                continuation is not None
                or execution.retry_strategy != RetryStrategy.NOT_RETRYABLE
            ),
        }
    await persist_execution_event(
        session,
        execution_id,
        "execution.operation_completed",
        {
            "status": operation.status.value,
            "execution_status": execution.status.value,
            "operation": {
                "id": str(operation.id),
                "number": operation.operation_number,
            },
            "step_summary": {
                "total": len(steps),
                "completed": completed,
                "succeeded": counts[StepStatus.SUCCEEDED],
                "failed": counts[StepStatus.FAILED],
                "cancelled": counts[StepStatus.CANCELLED],
            },
            "step_results": step_results,
            "continuation": continuation,
            "error": error,
        },
    )


async def add_execution_completed_event(
    session: AsyncSession, execution_id: UUID
) -> None:
    execution = await session.get(ExecutionORM, execution_id)
    if execution is None:
        return
    operations = list(
        await session.scalars(
            select(ExecutionOperationORM).where(
                ExecutionOperationORM.execution_id == execution_id
            )
        )
    )
    operation_counts = {
        status: sum(operation.status == status for operation in operations)
        for status in {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
        }
    }
    failed_step = None
    if execution.retry_from_sequence is not None:
        failed_step = await session.scalar(
            select(ExecutionStepORM).where(
                ExecutionStepORM.execution_id == execution_id,
                ExecutionStepORM.sequence == execution.retry_from_sequence,
            )
        )
    retry = None
    retry_deadline = (
        execution.retained_runtime_session_until
        or execution.execution_expires_at
    )
    if (
        execution.status == ExecutionStatus.FAILED
        and execution.retry_strategy != RetryStrategy.NOT_RETRYABLE
        and failed_step is not None
        and retry_deadline is not None
    ):
        retry = {
            "allowed": True,
            "from_step_id": str(failed_step.id),
            "expires_at": retry_deadline,
        }
    error = None
    if execution.status != ExecutionStatus.SUCCEEDED:
        active_operation = (
            await session.get(
                ExecutionOperationORM, execution.active_operation_id
            )
            if execution.active_operation_id is not None
            else None
        )
        error = {
            "code": (
                "EXECUTION_CANCELLED"
                if execution.status == ExecutionStatus.CANCELLED
                else "EXECUTION_"
                + (
                    execution.failure_type.value
                    if execution.failure_type is not None
                    else "FAILED"
                )
            ),
            "message": execution.error_message
            or execution.cancellation_reason
            or f"Execution was {execution.status.value.lower()}.",
            "operation_id": (
                str(active_operation.id) if active_operation else None
            ),
            "step_id": str(failed_step.id) if failed_step else None,
            "retryable": retry is not None,
        }
    await persist_execution_event(
        session,
        execution_id,
        "execution.completed",
        {
            "status": execution.status.value,
            "operation_summary": {
                "total": len(operations),
                "succeeded": operation_counts[OperationStatus.SUCCEEDED],
                "failed": operation_counts[OperationStatus.FAILED],
                "cancelled": operation_counts[OperationStatus.CANCELLED],
            },
            "retry": retry,
            "error": error,
        },
    )
