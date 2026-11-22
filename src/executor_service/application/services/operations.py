"""Incremental Execution Operation command handler."""

from executor_service.application.commands import CreateOperationCommand
from executor_service.application.services.support import (
    OPERATION_CREATE_LOCATION,
    ExecutionCommandSupport,
    apply_actor,
    code_hash,
    ensure_same_receipt,
    fingerprint,
    operation_id_from_receipt,
    required_execution,
    validate_operation_steps,
    validate_schema_version,
)
from executor_service.application.services.types import (
    ExecutionCommandResult,
)
from executor_service.domain.errors import (
    ExecutionNotFoundError,
    IdempotencyConflictError,
    PersistenceConflictError,
)
from executor_service.domain.models import (
    Execution,
    ExecutionOperation,
    ExecutionStep,
)
from executor_service.domain.ports import UnitOfWork


class ExecutionOperationCommands:
    def __init__(self, support: ExecutionCommandSupport) -> None:
        self._support = support

    async def create(
        self, command: CreateOperationCommand
    ) -> ExecutionCommandResult:
        validate_schema_version(
            command.spec_schema_version, (*OPERATION_CREATE_LOCATION, "spec")
        )
        request_fingerprint = fingerprint(command)
        command_type = "execution_operation_create"
        # Answer a repeat and refuse what cannot be accepted before any
        # source is written.
        async with self._support.uow_factory() as uow:
            repeated = await self._repeated(
                uow, command, command_type, request_fingerprint
            )
            if repeated is not None:
                return repeated
            self._accept(
                await uow.executions.get(command.execution_id), command
            )
        operation = ExecutionOperation(
            execution_id=command.execution_id,
            # Numbered under the row lock below.
            operation_number=0,
            first_sequence=command.steps[0].sequence,
            last_sequence=command.steps[-1].sequence,
            operation_timeout_seconds=command.operation_timeout_seconds,
            metadata=command.metadata,
            idempotency_key=command.idempotency_key,
            request_fingerprint=request_fingerprint,
            schema_version=command.spec_schema_version,
            created_by_client=command.actor_client,
            created_by=command.actor_id,
            updated_by_client=command.actor_client,
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
                created_by_client=command.actor_client,
                created_by=command.actor_id,
                updated_by_client=command.actor_client,
                updated_by=command.actor_id,
            )
            for source in command.steps
        ]
        # Shared-storage writes hold neither a pooled connection nor the
        # Execution row lock; the lock below checks the state again.
        await self._support.snapshot_sources(
            steps, command.execution_id, operation.id
        )
        try:
            async with self._support.uow_factory() as uow:
                repeated = await self._repeated(
                    uow, command, command_type, request_fingerprint
                )
                if repeated is not None:
                    return repeated
                execution = self._accept(
                    await uow.executions.get(
                        command.execution_id, for_update=True
                    ),
                    command,
                )
                apply_actor(execution, command.actor_client, command.actor_id)
                operation.operation_number = (
                    await uow.executions.next_operation_number(execution.id)
                )
                execution.steps.extend(steps)
                execution.active_operation_id = operation.id
                await uow.executions.save(execution)
                await uow.executions.add_operation(operation)
                for step in steps:
                    await uow.executions.add_step(execution.id, step)
                await uow.executions.add_command_receipt(
                    execution.id,
                    command.idempotency_key,
                    command_type,
                    request_fingerprint,
                    {
                        "execution_id": str(execution.id),
                        "operation_id": str(operation.id),
                    },
                )
                await uow.commit()
                return ExecutionCommandResult(
                    execution=execution,
                    operation_id=operation.id,
                )
        except PersistenceConflictError as exc:
            async with self._support.uow_factory() as uow:
                repeated = await self._repeated(
                    uow, command, command_type, request_fingerprint
                )
                if repeated is not None:
                    return repeated
            raise IdempotencyConflictError(
                "The command conflicted with another state change."
            ) from exc

    @staticmethod
    async def _repeated(
        uow: UnitOfWork,
        command: CreateOperationCommand,
        command_type: str,
        request_fingerprint: str,
    ) -> ExecutionCommandResult | None:
        """The stored answer when this idempotency key already ran."""
        receipt = await uow.executions.get_command_receipt(
            command.execution_id, command.idempotency_key
        )
        if receipt is None:
            return None
        ensure_same_receipt(receipt, command_type, request_fingerprint)
        return ExecutionCommandResult(
            execution=await required_execution(uow, command.execution_id),
            operation_id=operation_id_from_receipt(receipt),
        )

    def _accept(
        self, execution: Execution | None, command: CreateOperationCommand
    ) -> Execution:
        """Move the Execution to QUEUED for this Operation, or refuse it."""
        if execution is None:
            raise ExecutionNotFoundError(
                f"Execution {command.execution_id} was not found."
            )
        # State and version first: a caller with a stale view gets the 409
        # that says so, not a 422 about the sequence it computed from that
        # view.
        execution.request_operation(command.expected_version)
        self._support.validate_step_limits(
            len(execution.steps),
            len(command.steps),
            location=OPERATION_CREATE_LOCATION,
        )
        self._support.validate_timeouts(
            command.operation_timeout_seconds,
            (step.step_timeout_seconds for step in command.steps),
            location=OPERATION_CREATE_LOCATION,
        )
        validate_operation_steps(
            command.steps,
            expected_start=len(execution.steps),
            location=(*OPERATION_CREATE_LOCATION, "spec"),
        )
        return execution
