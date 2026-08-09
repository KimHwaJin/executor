"""Execution lifecycle use cases."""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict
from uuid import UUID

from executor_service.application.commands import (
    CancelExecutionCommand,
    ContinueExecutionCommand,
    FinishExecutionCommand,
    RetryExecutionCommand,
    SubmitExecutionCommand,
)
from executor_service.domain.enums import (
    CodeSourceType,
    ExecutionMode,
    ExecutionStatus,
    JupyterPool,
    TriggerType,
)
from executor_service.domain.errors import (
    ExecutionNotFoundError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PersistenceConflictError,
)
from executor_service.domain.models import Execution, ExecutionStep, OutboxEvent
from executor_service.domain.ports import UnitOfWork

UnitOfWorkFactory = Callable[[], UnitOfWork]


class ExecutionService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def submit(self, command: SubmitExecutionCommand) -> Execution:
        _validate_submit(command)
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
                            code=step.code,
                            code_hash=_code_hash(step.code),
                            plan_revision_id=step.plan_revision_id,
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

    async def continue_execution(self, command: ContinueExecutionCommand) -> Execution:
        fingerprint = _fingerprint(command)
        command_type = "execution_continue"
        try:
            async with self._uow_factory() as uow:
                repeated = await uow.executions.get_command_receipt(command.idempotency_key)
                if repeated is not None:
                    _ensure_same_receipt(repeated, command_type, fingerprint)
                    return await _required_execution(uow, command.execution_id)

                execution = await uow.executions.get(command.execution_id, for_update=True)
                if execution is None:
                    raise ExecutionNotFoundError(
                        f"Execution {command.execution_id} was not found."
                    )
                expected_sequence = len(execution.steps)
                if command.step.sequence != expected_sequence:
                    raise InvalidStateTransitionError(
                        f"Next dynamic step sequence must be {expected_sequence}."
                    )
                if not command.step.code or not command.step.code.strip():
                    raise InvalidStateTransitionError("Dynamic step code must not be empty.")
                execution.request_dynamic_continue(command.expected_version)
                step = ExecutionStep(
                    sequence=command.step.sequence,
                    code=command.step.code,
                    code_hash=_code_hash(command.step.code),
                    plan_revision_id=command.step.plan_revision_id,
                    skill_name=command.step.skill_name,
                    tool_name=command.step.tool_name,
                    input_parameters=command.step.input_parameters,
                )
                execution.steps.append(step)
                await uow.executions.save(execution)
                await uow.executions.add_step(execution.id, step)
                await uow.executions.add_command_receipt(
                    command.idempotency_key,
                    command_type,
                    fingerprint,
                    {"execution_id": str(execution.id)},
                )
                await uow.outbox.add(
                    OutboxEvent(
                        aggregate_type="Execution",
                        aggregate_id=execution.id,
                        event_type="execution.continue_requested",
                        payload={
                            "execution_id": str(execution.id),
                            "status": execution.status.value,
                            "sequence": step.sequence,
                            "version": execution.version,
                        },
                    )
                )
                await uow.commit()
                return execution
        except PersistenceConflictError as exc:
            return await self._resolve_repeated_command(
                command.idempotency_key,
                command_type,
                fingerprint,
                command.execution_id,
                exc,
            )

    async def finish_execution(self, command: FinishExecutionCommand) -> Execution:
        fingerprint = _fingerprint(command)
        command_type = "execution_finish"
        try:
            async with self._uow_factory() as uow:
                repeated = await uow.executions.get_command_receipt(command.idempotency_key)
                if repeated is not None:
                    _ensure_same_receipt(repeated, command_type, fingerprint)
                    return await _required_execution(uow, command.execution_id)
                execution = await uow.executions.get(command.execution_id, for_update=True)
                if execution is None:
                    raise ExecutionNotFoundError(
                        f"Execution {command.execution_id} was not found."
                    )
                execution.request_dynamic_finish(command.expected_version)
                await uow.executions.save(execution)
                await uow.executions.add_command_receipt(
                    command.idempotency_key,
                    command_type,
                    fingerprint,
                    {"execution_id": str(execution.id)},
                )
                await uow.outbox.add(
                    OutboxEvent(
                        aggregate_type="Execution",
                        aggregate_id=execution.id,
                        event_type="execution.finish_requested",
                        payload={
                            "execution_id": str(execution.id),
                            "status": execution.status.value,
                            "version": execution.version,
                        },
                    )
                )
                await uow.commit()
                return execution
        except PersistenceConflictError as exc:
            return await self._resolve_repeated_command(
                command.idempotency_key,
                command_type,
                fingerprint,
                command.execution_id,
                exc,
            )

    async def _resolve_repeated_command(
        self,
        idempotency_key: str,
        command_type: str,
        fingerprint: str,
        execution_id: UUID,
        cause: PersistenceConflictError,
    ) -> Execution:
        async with self._uow_factory() as uow:
            repeated = await uow.executions.get_command_receipt(idempotency_key)
            if repeated is not None:
                _ensure_same_receipt(repeated, command_type, fingerprint)
                return await _required_execution(uow, execution_id)
        raise IdempotencyConflictError(
            "The command conflicted with another state change."
        ) from cause

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


def _fingerprint(
    command: SubmitExecutionCommand | ContinueExecutionCommand | FinishExecutionCommand,
) -> str:
    payload = asdict(command)
    payload.pop("idempotency_key")
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _ensure_same_fingerprint(execution: Execution, fingerprint: str) -> None:
    if execution.request_fingerprint != fingerprint:
        raise IdempotencyConflictError(
            "The submit idempotency_key was already used with a different request."
        )


def _validate_submit(command: SubmitExecutionCommand) -> None:
    if command.mode != ExecutionMode.DYNAMIC:
        return
    if command.trigger_type != TriggerType.INTERACTIVE:
        raise InvalidStateTransitionError(
            "DYNAMIC execution requires INTERACTIVE trigger_type."
        )
    if command.jupyter_pool != JupyterPool.INTERACTIVE:
        raise InvalidStateTransitionError(
            "DYNAMIC execution requires INTERACTIVE jupyter_pool."
        )
    if command.code_source_type != CodeSourceType.INLINE:
        raise InvalidStateTransitionError("DYNAMIC execution requires INLINE code.")
    if len(command.steps) != 1 or command.steps[0].sequence != 0:
        raise InvalidStateTransitionError(
            "DYNAMIC submit requires exactly the first step (sequence 0)."
        )
    if command.steps[0].code != command.code:
        raise InvalidStateTransitionError(
            "DYNAMIC source code must match the first step code."
        )


def _code_hash(code: str | None) -> str | None:
    return hashlib.sha256(code.encode()).hexdigest() if code is not None else None


def _ensure_same_receipt(
    receipt: tuple[str, str, dict[str, object]], command_type: str, fingerprint: str
) -> None:
    receipt_type, receipt_fingerprint, _ = receipt
    if receipt_type != command_type or receipt_fingerprint != fingerprint:
        raise IdempotencyConflictError(
            "idempotency_key was already used with a different command."
        )


async def _required_execution(uow: UnitOfWork, execution_id: UUID) -> Execution:
    execution = await uow.executions.get(execution_id)
    if execution is None:
        raise ExecutionNotFoundError(f"Execution {execution_id} was not found.")
    return execution
