"""Incremental Execution Operation command handler."""

from executor_service.application._execution_service.support import (
    ExecutionCommandSupport,
    apply_actor,
    code_hash,
    ensure_same_receipt,
    fingerprint,
    operation_id_from_receipt,
    required_execution,
)
from executor_service.application._execution_service.types import (
    ExecutionCommandResult,
)
from executor_service.application.commands import CreateOperationCommand
from executor_service.domain.errors import (
    ExecutionNotFoundError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PersistenceConflictError,
)
from executor_service.domain.models import ExecutionOperation, ExecutionStep
from executor_service.work_messages import build_work_message


class ExecutionOperationCommands:
    def __init__(self, support: ExecutionCommandSupport) -> None:
        self._support = support

    async def create(
        self, command: CreateOperationCommand
    ) -> ExecutionCommandResult:
        if command.spec_schema_version != "1.0":
            raise InvalidStateTransitionError(
                "Only ExecutionSpec schema_version 1.0 is supported."
            )
        request_fingerprint = fingerprint(command)
        command_type = "execution_operation_create"
        try:
            async with self._support.uow_factory() as uow:
                repeated = await uow.executions.get_command_receipt(
                    command.idempotency_key
                )
                if repeated is not None:
                    ensure_same_receipt(
                        repeated, command_type, request_fingerprint
                    )
                    execution = await required_execution(
                        uow, command.execution_id
                    )
                    return ExecutionCommandResult(
                        execution=execution,
                        operation_id=operation_id_from_receipt(repeated),
                    )

                execution = await uow.executions.get(
                    command.execution_id, for_update=True
                )
                if execution is None:
                    raise ExecutionNotFoundError(
                        f"Execution {command.execution_id} was not found."
                    )
                if not command.steps:
                    raise InvalidStateTransitionError(
                        "An Operation requires at least one Step."
                    )
                self._support.validate_step_limits(
                    len(execution.steps), len(command.steps)
                )
                expected_sequence = len(execution.steps)
                sequences = [step.sequence for step in command.steps]
                expected_sequences = list(
                    range(
                        expected_sequence,
                        expected_sequence + len(command.steps),
                    )
                )
                if sequences != expected_sequences:
                    raise InvalidStateTransitionError(
                        "MULTI Operation Step sequences must be contiguous "
                        f"and start at {expected_sequence}."
                    )
                if any(not step.code.strip() for step in command.steps):
                    raise InvalidStateTransitionError(
                        "Operation Step code must not be empty."
                    )
                execution.request_operation(command.expected_version)
                apply_actor(execution, command.actor_type, command.actor_id)
                operation = ExecutionOperation(
                    execution_id=execution.id,
                    operation_number=(
                        await uow.executions.next_operation_number(
                            execution.id
                        )
                    ),
                    first_sequence=command.steps[0].sequence,
                    last_sequence=command.steps[-1].sequence,
                    operation_timeout_seconds=(
                        command.operation_timeout_seconds
                    ),
                    metadata=command.metadata,
                    idempotency_key=command.idempotency_key,
                    request_fingerprint=request_fingerprint,
                    schema_version=command.spec_schema_version,
                    created_by_type=command.actor_type,
                    created_by=command.actor_id,
                    updated_by_type=command.actor_type,
                    updated_by=command.actor_id,
                )
                steps = [
                    ExecutionStep(
                        sequence=source.sequence,
                        code=source.code,
                        source_type=source.source_type,
                        source_path=source.source_path,
                        source_sha256=source.source_sha256,
                        step_timeout_seconds=source.step_timeout_seconds,
                        code_hash=code_hash(source.code),
                        skill_name=source.skill_name,
                        tool_name=source.tool_name,
                        input_parameters=source.input_parameters,
                        operation_id=operation.id,
                        created_by_type=command.actor_type,
                        created_by=command.actor_id,
                        updated_by_type=command.actor_type,
                        updated_by=command.actor_id,
                    )
                    for source in command.steps
                ]
                await self._support.snapshot_sources(steps, execution.id)
                execution.steps.extend(steps)
                execution.active_operation_id = operation.id
                await uow.executions.save(execution)
                await uow.executions.add_operation(operation)
                for step in steps:
                    await uow.executions.add_step(execution.id, step)
                await uow.executions.add_command_receipt(
                    command.idempotency_key,
                    command_type,
                    request_fingerprint,
                    {
                        "execution_id": str(execution.id),
                        "operation_id": str(operation.id),
                    },
                )
                await uow.outbox.add(
                    build_work_message(
                        execution_id=execution.id,
                        message_type="operation.ready",
                        operation_id=operation.id,
                        actor_type=command.actor_type,
                        actor_id=command.actor_id,
                    )
                )
                await uow.commit()
                return ExecutionCommandResult(
                    execution=execution,
                    operation_id=operation.id,
                )
        except PersistenceConflictError as exc:
            async with self._support.uow_factory() as uow:
                repeated = await uow.executions.get_command_receipt(
                    command.idempotency_key
                )
                if repeated is not None:
                    ensure_same_receipt(
                        repeated, command_type, request_fingerprint
                    )
                    execution = await required_execution(
                        uow, command.execution_id
                    )
                    return ExecutionCommandResult(
                        execution=execution,
                        operation_id=operation_id_from_receipt(repeated),
                    )
            raise IdempotencyConflictError(
                "The command conflicted with another state change."
            ) from exc
