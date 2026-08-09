"""Execution lifecycle use cases."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from uuid import UUID

from executor_service.application.commands import (
    CancelExecutionCommand,
    RetryExecutionCommand,
    SubmitExecutionCommand,
)
from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.errors import (
    ExecutionNotFoundError,
    IdempotencyConflictError,
    PersistenceConflictError,
)
from executor_service.domain.models import Execution, ExecutionStep, OutboxEvent
from executor_service.domain.ports import UnitOfWork

UnitOfWorkFactory = Callable[[], UnitOfWork]


class ExecutionService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def submit(self, command: SubmitExecutionCommand) -> Execution:
        fingerprint = _fingerprint(command)
        try:
            async with self._uow_factory() as uow:
                existing = await uow.executions.get_by_submit_key(command.idempotency_key)
                if existing is not None:
                    _ensure_same_fingerprint(existing, fingerprint)
                    return existing

                execution = Execution(
                    idempotency_key=command.idempotency_key,
                    request_fingerprint=fingerprint,
                    mode=command.mode,
                    trigger_type=command.trigger_type,
                    jupyter_pool=command.jupyter_pool,
                    kernel_name=command.kernel_name,
                    code_source_type=command.code_source_type,
                    code=command.code,
                    code_path=command.code_path,
                    requested_by_user_id=command.requested_by_user_id,
                    project_id=command.project_id,
                    session_id=command.session_id,
                    execution_plan_id=command.execution_plan_id,
                    workflow_id=command.workflow_id,
                    correlation_id=command.correlation_id,
                    metadata=command.metadata,
                    steps=[
                        ExecutionStep(
                            sequence=step.sequence,
                            skill_name=step.skill_name,
                            tool_name=step.tool_name,
                            input_parameters=step.input_parameters,
                        )
                        for step in command.steps
                    ],
                )
                await uow.executions.add(execution)
                await uow.outbox.add(
                    OutboxEvent(
                        aggregate_type="Execution",
                        aggregate_id=execution.id,
                        event_type="execution.submitted",
                        payload={
                            "execution_id": str(execution.id),
                            "status": execution.status.value,
                        },
                    )
                )
                await uow.commit()
                return execution
        except PersistenceConflictError:
            # A concurrent request may have committed the same key after our first lookup.
            async with self._uow_factory() as uow:
                existing = await uow.executions.get_by_submit_key(command.idempotency_key)
                if existing is None:
                    raise
                _ensure_same_fingerprint(existing, fingerprint)
                return existing

    async def get(self, execution_id: UUID) -> Execution:
        async with self._uow_factory() as uow:
            execution = await uow.executions.get(execution_id)
            if execution is None:
                raise ExecutionNotFoundError(f"Execution {execution_id} was not found.")
            return execution

    async def cancel(self, command: CancelExecutionCommand) -> Execution:
        try:
            async with self._uow_factory() as uow:
                key_owner = await uow.executions.get_by_cancel_key(command.idempotency_key)
                if key_owner is not None:
                    if key_owner.id != command.execution_id:
                        raise IdempotencyConflictError(
                            "The cancel idempotency_key was already used for another execution."
                        )
                    return key_owner

                execution = await uow.executions.get(command.execution_id, for_update=True)
                if execution is None:
                    raise ExecutionNotFoundError(f"Execution {command.execution_id} was not found.")
                if execution.status == ExecutionStatus.CANCEL_REQUESTED:
                    return execution

                execution.request_cancel(command.idempotency_key, command.reason)
                await uow.executions.save(execution)
                await uow.outbox.add(
                    OutboxEvent(
                        aggregate_type="Execution",
                        aggregate_id=execution.id,
                        event_type="execution.cancel_requested",
                        payload={
                            "execution_id": str(execution.id),
                            "status": execution.status.value,
                        },
                    )
                )
                await uow.commit()
                return execution
        except PersistenceConflictError as exc:
            async with self._uow_factory() as uow:
                key_owner = await uow.executions.get_by_cancel_key(command.idempotency_key)
                if key_owner is not None and key_owner.id == command.execution_id:
                    return key_owner
            raise IdempotencyConflictError(
                "The cancel request conflicted with another state change."
            ) from exc

    async def retry(self, command: RetryExecutionCommand) -> Execution:
        try:
            async with self._uow_factory() as uow:
                key_owner = await uow.executions.get_by_retry_key(command.idempotency_key)
                if key_owner is not None:
                    if key_owner.id != command.execution_id:
                        raise IdempotencyConflictError(
                            "The retry idempotency_key was already used for another execution."
                        )
                    return key_owner

                execution = await uow.executions.get(command.execution_id, for_update=True)
                if execution is None:
                    raise ExecutionNotFoundError(
                        f"Execution {command.execution_id} was not found."
                    )
                execution.request_retry()
                if execution.retry_from_sequence is None:
                    raise RuntimeError("Retry sequence unexpectedly missing.")
                await uow.executions.save(execution)
                await uow.executions.add_retry_receipt(
                    execution.id,
                    command.idempotency_key,
                    execution.retry_from_sequence,
                )
                await uow.outbox.add(
                    OutboxEvent(
                        aggregate_type="Execution",
                        aggregate_id=execution.id,
                        event_type="execution.retry_requested",
                        payload={
                            "execution_id": str(execution.id),
                            "status": execution.status.value,
                            "from_sequence": execution.retry_from_sequence,
                            "retry_strategy": execution.retry_strategy.value,
                            "previous_failure_type": (
                                execution.failure_type.value
                                if execution.failure_type is not None
                                else None
                            ),
                            "retry_count": execution.retry_count,
                        },
                    )
                )
                await uow.commit()
                return execution
        except PersistenceConflictError as exc:
            async with self._uow_factory() as uow:
                key_owner = await uow.executions.get_by_retry_key(command.idempotency_key)
                if key_owner is not None and key_owner.id == command.execution_id:
                    return key_owner
            raise IdempotencyConflictError(
                "The retry request conflicted with another state change."
            ) from exc


def _fingerprint(command: SubmitExecutionCommand) -> str:
    payload = asdict(command)
    payload.pop("idempotency_key")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _ensure_same_fingerprint(execution: Execution, fingerprint: str) -> None:
    if execution.request_fingerprint != fingerprint:
        raise IdempotencyConflictError(
            "The submit idempotency_key was already used with a different request."
        )
