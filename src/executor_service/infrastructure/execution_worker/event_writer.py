"""Persist ordered public Execution events and their Outbox records."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.domain.enums import (
    ExecutionStatus,
    OperationStatus,
    OutboxDestination,
    RetryStrategy,
    StepStatus,
)
from executor_service.domain.results import StepResultDescriptor
from executor_service.events import build_execution_event
from executor_service.infrastructure.db.models import (
    ExecutionAttemptORM,
    ExecutionEventORM,
    ExecutionEventSequenceORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    OutboxEventORM,
)
from executor_service.infrastructure.execution_leases import ExecutionLease
from executor_service.tracing import capture_trace_carrier


async def persist_execution_event(
    session: AsyncSession,
    execution_id: UUID,
    event_type: str,
    payload: dict[str, object],
) -> None:
    execution = await session.get(ExecutionORM, execution_id)
    if execution is None:
        raise ValueError(f"Execution {execution_id} was not found.")
    event_sequence = await _next_execution_event_sequence(
        session, execution_id
    )
    actor_type = execution.updated_by_type or execution.created_by_type
    actor_id = execution.updated_by or execution.created_by
    carrier = capture_trace_carrier()
    event = build_execution_event(
        execution_id=execution_id,
        event_sequence=event_sequence,
        event_type=event_type,
        payload=payload,
        actor_type=actor_type,
        actor_id=actor_id,
        traceparent=carrier.traceparent,
        tracestate=carrier.tracestate,
    )
    session.add(ExecutionEventORM.from_domain(event))
    session.add(OutboxEventORM.from_execution_event(event))


async def _next_execution_event_sequence(
    session: AsyncSession,
    execution_id: UUID,
) -> int:
    table = ExecutionEventSequenceORM.__table__
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        statement = postgresql_insert(ExecutionEventSequenceORM)
    elif dialect_name == "sqlite":
        statement = sqlite_insert(ExecutionEventSequenceORM)
    else:
        raise RuntimeError(
            f"Unsupported event sequence dialect: {dialect_name}"
        )
    result = await session.execute(
        statement.values(
            execution_id=execution_id,
            last_sequence=1,
        )
        .on_conflict_do_update(
            index_elements=[table.c.execution_id],
            set_={"last_sequence": table.c.last_sequence + 1},
        )
        .returning(table.c.last_sequence)
    )
    return int(result.scalar_one())


async def add_start_events(session: AsyncSession, execution_id: UUID) -> None:
    execution = await session.get(ExecutionORM, execution_id)
    if (
        execution is None
        or execution.runtime_target_id is None
        or execution.runtime_session_id is None
    ):
        return
    started_count = await session.scalar(
        select(func.count(OutboxEventORM.id)).where(
            OutboxEventORM.aggregate_id == execution_id,
            OutboxEventORM.destination == OutboxDestination.EVENTS,
            OutboxEventORM.event_type == "execution.started",
        )
    )
    if not started_count:
        await persist_execution_event(
            session,
            execution_id,
            "execution.started",
            {
                "status": "RUNNING",
                "runtime": {
                    "provider": execution.runtime_type.value,
                    "profile": execution.runtime_profile,
                    "target_id": str(execution.runtime_target_id),
                    "session_id": execution.runtime_session_id,
                },
            },
        )
    operation_id = execution.active_operation_id
    if operation_id is None or execution.finalization_requested:
        return
    operation = await session.get(ExecutionOperationORM, operation_id)
    if operation is None:
        return
    prior_payloads = list(
        await session.scalars(
            select(ExecutionEventORM.payload).where(
                ExecutionEventORM.execution_id == execution_id,
                ExecutionEventORM.event_type == "execution.operation_started",
            )
        )
    )
    if any(
        str(payload.get("operation", {}).get("id")) == str(operation_id)
        for payload in prior_payloads
        if isinstance(payload.get("operation"), dict)
    ):
        return
    step_count = await session.scalar(
        select(func.count(ExecutionStepORM.id)).where(
            ExecutionStepORM.operation_id == operation_id
        )
    )
    await persist_execution_event(
        session,
        execution_id,
        "execution.operation_started",
        {
            "status": "RUNNING",
            "operation": {
                "id": str(operation.id),
                "number": operation.operation_number,
                "step_count": step_count or 0,
            },
        },
    )


async def _attempt_payload(
    session: AsyncSession, attempt_id: UUID
) -> dict[str, object]:
    attempt = await session.get(ExecutionAttemptORM, attempt_id)
    if attempt is None:
        raise ValueError(f"Execution Attempt {attempt_id} was not found.")
    return {
        "id": str(attempt.id),
        "number": attempt.attempt_number,
        "reason": ("INITIAL" if attempt.attempt_number == 1 else "RETRY"),
    }


async def _operation_payload(
    session: AsyncSession, operation_id: UUID
) -> dict[str, object]:
    operation = await session.get(ExecutionOperationORM, operation_id)
    if operation is None:
        raise ValueError(f"Execution Operation {operation_id} was not found.")
    return {"id": str(operation.id), "number": operation.operation_number}


def _stored_result_reference(
    stored_result: StepResultDescriptor,
) -> dict[str, object] | None:
    if not stored_result.complete:
        return None
    return {
        "storage": "SHARED_PV",
        "relative_path": stored_result.reference.relative_path,
        "media_type": "application/json",
        "size_bytes": stored_result.reference.size_bytes,
        "checksum_sha256": stored_result.reference.checksum_sha256,
    }


def _row_result_reference(
    row: ExecutionStepAttemptORM,
) -> dict[str, object] | None:
    if not (
        row.result_complete
        and row.result_manifest_path is not None
        and row.result_manifest_checksum_sha256 is not None
        and row.result_manifest_size_bytes is not None
    ):
        return None
    return {
        "storage": "SHARED_PV",
        "relative_path": row.result_manifest_path,
        "media_type": "application/json",
        "size_bytes": row.result_manifest_size_bytes,
        "checksum_sha256": row.result_manifest_checksum_sha256,
    }


def _event_output_summary(
    stored_result: StepResultDescriptor,
) -> dict[str, object]:
    mime_types = stored_result.output_summary.get("mime_types", [])
    return {
        "count": stored_result.output_count,
        "content_types": (
            sorted(set(mime_types)) if isinstance(mime_types, list) else []
        ),
    }


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
            "operation": await _operation_payload(session, step.operation_id),
            "step": {"id": str(step.id), "sequence": step.sequence},
            "attempt": await _attempt_payload(session, lease.attempt_id),
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
        _stored_result_reference(stored_result)
        if stored_result is not None
        else None
    )
    output_summary = (
        _event_output_summary(stored_result)
        if stored_result is not None and stored_result.complete
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
            "operation": await _operation_payload(session, step.operation_id),
            "step": {"id": str(step.id), "sequence": step.sequence},
            "attempt": await _attempt_payload(session, lease.attempt_id),
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
    output_summary = None
    if history.result_complete:
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
            "operation": await _operation_payload(session, step.operation_id),
            "step": {"id": str(step.id), "sequence": step.sequence},
            "attempt": await _attempt_payload(session, attempt_id),
            "result_ref": _row_result_reference(history),
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


async def _latest_step_attempts(
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
    latest = await _latest_step_attempts(session, operation_id)
    step_results: list[dict[str, object]] = []
    for step in steps:
        attempt_pair = latest.get(step.id)
        if step.status not in terminal or attempt_pair is None:
            continue
        history, attempt = attempt_pair
        result_ref = _row_result_reference(history)
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
