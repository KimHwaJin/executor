"""Execution lifecycle command handler."""

from uuid import UUID

from executor_service.application._execution_service.support import (
    ExecutionCommandSupport,
    apply_actor,
    apply_current_trace,
    apply_step_actor,
    ensure_same_receipt,
    fingerprint,
    required_execution,
    required_operation_id,
)
from executor_service.application._execution_service.types import (
    ExecutionCommandResult,
)
from executor_service.application.commands import (
    CancelExecutionCommand,
    FinalizeExecutionCommand,
    RetryExecutionCommand,
)
from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.errors import (
    ExecutionNotFoundError,
    IdempotencyConflictError,
    PersistenceConflictError,
)
from executor_service.domain.models import Execution
from executor_service.work_messages import build_work_message


class ExecutionLifecycleCommands:
    def __init__(self, support: ExecutionCommandSupport) -> None:
        self._support = support

    async def get(self, execution_id: UUID) -> Execution:
        async with self._support.uow_factory() as uow:
            execution = await uow.executions.get(execution_id)
            if execution is None:
                raise ExecutionNotFoundError(
                    f"Execution {execution_id} was not found."
                )
            return execution

    async def finalize(self, command: FinalizeExecutionCommand) -> Execution:
        request_fingerprint = fingerprint(command)
        command_type = "execution_finalize"
        try:
            async with self._support.uow_factory() as uow:
                repeated = await uow.executions.get_command_receipt(
                    command.idempotency_key
                )
                if repeated is not None:
                    ensure_same_receipt(
                        repeated, command_type, request_fingerprint
                    )
                    return await required_execution(uow, command.execution_id)
                execution = await uow.executions.get(
                    command.execution_id, for_update=True
                )
                if execution is None:
                    raise ExecutionNotFoundError(
                        f"Execution {command.execution_id} was not found."
                    )
                execution.request_finalization(command.expected_version)
                apply_actor(execution, command.actor_type, command.actor_id)
                apply_current_trace(execution)
                await uow.executions.save(execution)
                await uow.executions.add_command_receipt(
                    command.idempotency_key,
                    command_type,
                    request_fingerprint,
                    {"execution_id": str(execution.id)},
                )
                await uow.outbox.add(
                    build_work_message(
                        execution_id=execution.id,
                        message_type="execution.finalization_ready",
                        actor_type=command.actor_type,
                        actor_id=command.actor_id,
                        traceparent=execution.traceparent,
                        tracestate=execution.tracestate,
                    )
                )
                await uow.commit()
                return execution
        except PersistenceConflictError as exc:
            return await self._resolve_repeated_command(
                command.idempotency_key,
                command_type,
                request_fingerprint,
                command.execution_id,
                exc,
            )

    async def cancel(self, command: CancelExecutionCommand) -> Execution:
        try:
            async with self._support.uow_factory() as uow:
                key_owner = await uow.executions.get_by_cancel_key(
                    command.idempotency_key
                )
                if key_owner is not None:
                    if key_owner.id != command.execution_id:
                        raise IdempotencyConflictError(
                            "The cancel idempotency_key was already used for "
                            "another execution."
                        )
                    return key_owner

                execution = await uow.executions.get(
                    command.execution_id, for_update=True
                )
                if execution is None:
                    raise ExecutionNotFoundError(
                        f"Execution {command.execution_id} was not found."
                    )
                if execution.status == ExecutionStatus.CANCEL_REQUESTED:
                    return execution

                execution.request_cancel(
                    command.idempotency_key, command.reason
                )
                apply_actor(execution, command.actor_type, command.actor_id)
                apply_current_trace(execution)
                await uow.executions.save(execution)
                await uow.outbox.add(
                    build_work_message(
                        execution_id=execution.id,
                        message_type="execution.cancellation_ready",
                        actor_type=command.actor_type,
                        actor_id=command.actor_id,
                        traceparent=execution.traceparent,
                        tracestate=execution.tracestate,
                    )
                )
                await uow.commit()
                return execution
        except PersistenceConflictError as exc:
            async with self._support.uow_factory() as uow:
                key_owner = await uow.executions.get_by_cancel_key(
                    command.idempotency_key
                )
                if (
                    key_owner is not None
                    and key_owner.id == command.execution_id
                ):
                    return key_owner
            raise IdempotencyConflictError(
                "The cancel request conflicted with another state change."
            ) from exc

    async def retry(
        self, command: RetryExecutionCommand
    ) -> ExecutionCommandResult:
        try:
            async with self._support.uow_factory() as uow:
                key_owner = await uow.executions.get_by_retry_key(
                    command.idempotency_key
                )
                if key_owner is not None:
                    if key_owner.id != command.execution_id:
                        raise IdempotencyConflictError(
                            "The retry idempotency_key was already used for "
                            "another execution."
                        )
                    return ExecutionCommandResult(
                        execution=key_owner,
                        operation_id=required_operation_id(
                            key_owner.active_operation_id
                        ),
                    )

                execution = await uow.executions.get(
                    command.execution_id, for_update=True
                )
                if execution is None:
                    raise ExecutionNotFoundError(
                        f"Execution {command.execution_id} was not found."
                    )
                execution.request_retry()
                operation_id = required_operation_id(
                    execution.active_operation_id
                )
                apply_actor(execution, command.actor_type, command.actor_id)
                for step in execution.steps:
                    if (
                        execution.retry_from_sequence is not None
                        and step.sequence >= execution.retry_from_sequence
                    ):
                        apply_step_actor(
                            step, command.actor_type, command.actor_id
                        )
                apply_current_trace(execution)
                if execution.retry_from_sequence is None:
                    raise RuntimeError("Retry sequence unexpectedly missing.")
                await uow.executions.save(execution)
                await uow.executions.requeue_operation_for_retry(
                    operation_id,
                    updated_by_type=command.actor_type,
                    updated_by=command.actor_id,
                )
                await uow.executions.add_retry_receipt(
                    execution.id,
                    command.idempotency_key,
                    execution.retry_from_sequence,
                )
                await uow.outbox.add(
                    build_work_message(
                        execution_id=execution.id,
                        message_type="execution.retry_ready",
                        operation_id=operation_id,
                        actor_type=command.actor_type,
                        actor_id=command.actor_id,
                        traceparent=execution.traceparent,
                        tracestate=execution.tracestate,
                    )
                )
                await uow.commit()
                return ExecutionCommandResult(
                    execution=execution,
                    operation_id=operation_id,
                )
        except PersistenceConflictError as exc:
            async with self._support.uow_factory() as uow:
                key_owner = await uow.executions.get_by_retry_key(
                    command.idempotency_key
                )
                if (
                    key_owner is not None
                    and key_owner.id == command.execution_id
                ):
                    return ExecutionCommandResult(
                        execution=key_owner,
                        operation_id=required_operation_id(
                            key_owner.active_operation_id
                        ),
                    )
            raise IdempotencyConflictError(
                "The retry request conflicted with another state change."
            ) from exc

    async def _resolve_repeated_command(
        self,
        idempotency_key: str,
        command_type: str,
        request_fingerprint: str,
        execution_id: UUID,
        cause: PersistenceConflictError,
    ) -> Execution:
        async with self._support.uow_factory() as uow:
            repeated = await uow.executions.get_command_receipt(
                idempotency_key
            )
            if repeated is not None:
                ensure_same_receipt(
                    repeated, command_type, request_fingerprint
                )
                return await required_execution(uow, execution_id)
        raise IdempotencyConflictError(
            "The command conflicted with another state change."
        ) from cause
