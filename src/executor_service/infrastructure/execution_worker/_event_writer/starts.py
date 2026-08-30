"""Execution and Operation start event builders."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from executor_service.domain.enums import OutboxDestination
from executor_service.infrastructure.db.models import (
    ExecutionEventORM,
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepORM,
    OutboxEventORM,
)
from executor_service.infrastructure.execution_worker._event_writer.persistence import (
    persist_execution_event,
)


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
