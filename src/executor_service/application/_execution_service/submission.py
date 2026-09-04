"""Execution submission command handler."""

from collections.abc import Mapping
from uuid import UUID

from executor_service.application._execution_service.support import (
    ExecutionCommandSupport,
    code_hash,
    ensure_same_fingerprint,
    fingerprint,
    required_operation_id,
    validate_submit,
)
from executor_service.application._execution_service.types import (
    ExecutionCommandResult,
)
from executor_service.application.commands import SubmitExecutionCommand
from executor_service.domain.enums import RuntimePool, RuntimeType
from executor_service.domain.errors import (
    PersistenceConflictError,
    UnsupportedRuntimeProfileError,
)
from executor_service.domain.models import (
    Execution,
    ExecutionOperation,
    ExecutionStep,
)
from executor_service.work_messages import build_work_message


class ExecutionSubmissionCommands:
    def __init__(
        self,
        support: ExecutionCommandSupport,
        runtime_profiles: Mapping[RuntimeType, tuple[str, ...]],
    ) -> None:
        self._support = support
        self._runtime_profiles = {
            runtime_type: tuple(profiles)
            for runtime_type, profiles in runtime_profiles.items()
        }

    @property
    def runtime_profiles(self) -> dict[str, tuple[str, ...]]:
        return {
            runtime_type.value: profiles
            for runtime_type, profiles in self._runtime_profiles.items()
        }

    async def submit(
        self, command: SubmitExecutionCommand
    ) -> ExecutionCommandResult:
        validate_submit(command)
        self._support.validate_step_limits(0, len(command.steps))
        allowed_profiles = self._runtime_profiles.get(command.runtime_type, ())
        if command.runtime_profile not in allowed_profiles:
            raise UnsupportedRuntimeProfileError(
                f"runtime_profile '{command.runtime_profile}' is not supported "
                f"for runtime_type '{command.runtime_type.value}'."
            )
        request_fingerprint = fingerprint(command)
        try:
            async with self._support.uow_factory() as uow:
                existing = await uow.executions.get_by_submit_key(
                    command.idempotency_key
                )
                if existing is not None:
                    ensure_same_fingerprint(existing, request_fingerprint)
                    operation_id = (
                        await uow.executions.get_operation_id_by_key(
                            command.idempotency_key
                        )
                    )
                    return ExecutionCommandResult(
                        execution=existing,
                        operation_id=required_operation_id(operation_id),
                    )

                operation = ExecutionOperation(
                    execution_id=UUID(int=0),
                    operation_number=1,
                    first_sequence=command.steps[0].sequence,
                    last_sequence=command.steps[-1].sequence,
                    operation_timeout_seconds=(
                        command.operation_timeout_seconds
                    ),
                    metadata=command.operation_metadata,
                    idempotency_key=command.idempotency_key,
                    request_fingerprint=request_fingerprint,
                    schema_version=command.spec_schema_version,
                    created_by_type=command.actor_type,
                    created_by=command.actor_id,
                    updated_by_type=command.actor_type,
                    updated_by=command.actor_id,
                )
                execution = Execution(
                    idempotency_key=command.idempotency_key,
                    request_fingerprint=request_fingerprint,
                    operation_mode=command.operation_mode,
                    operation_wait_timeout_seconds=(
                        command.operation_wait_timeout_seconds
                    ),
                    trigger_type=command.trigger_type,
                    runtime_type=command.runtime_type,
                    runtime_pool=RuntimePool(command.trigger_type.value),
                    runtime_profile=command.runtime_profile,
                    user_id=command.user_id,
                    project_id=command.project_id,
                    session_id=command.session_id,
                    task_id=command.task_id,
                    workflow_id=command.workflow_id,
                    created_by_type=command.actor_type,
                    created_by=command.actor_id,
                    updated_by_type=command.actor_type,
                    updated_by=command.actor_id,
                    metadata=command.metadata,
                    steps=[
                        ExecutionStep(
                            sequence=step.sequence,
                            code=step.code,
                            source_type=step.source_type,
                            source_path=step.source_path,
                            source_sha256=step.source_sha256,
                            step_timeout_seconds=step.step_timeout_seconds,
                            code_hash=code_hash(step.code),
                            skill_name=step.skill_name,
                            tool_name=step.tool_name,
                            input_parameters=step.input_parameters,
                            operation_id=operation.id,
                            created_by_type=command.actor_type,
                            created_by=command.actor_id,
                            updated_by_type=command.actor_type,
                            updated_by=command.actor_id,
                        )
                        for step in command.steps
                    ],
                    active_operation_id=operation.id,
                )
                operation.execution_id = execution.id
                await self._support.snapshot_sources(
                    execution.steps, execution.id
                )
                await uow.executions.add(execution)
                await uow.executions.add_operation(operation)
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
        except PersistenceConflictError:
            # A concurrent request may have committed the same key after our
            # first lookup.
            async with self._support.uow_factory() as uow:
                existing = await uow.executions.get_by_submit_key(
                    command.idempotency_key
                )
                if existing is None:
                    raise
                ensure_same_fingerprint(existing, request_fingerprint)
                operation_id = await uow.executions.get_operation_id_by_key(
                    command.idempotency_key
                )
                return ExecutionCommandResult(
                    execution=existing,
                    operation_id=required_operation_id(operation_id),
                )
