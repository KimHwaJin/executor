"""Shared validation and persistence support for Execution commands."""

import hashlib
import json
from dataclasses import asdict
from uuid import UUID

from executor_service.application._execution_service.types import (
    UnitOfWorkFactory,
)
from executor_service.application.commands import (
    CreateOperationCommand,
    FinalizeExecutionCommand,
    SubmitExecutionCommand,
)
from executor_service.domain.enums import (
    ActorType,
    OperationMode,
    TriggerType,
)
from executor_service.domain.errors import (
    ExecutionNotFoundError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PersistenceConflictError,
)
from executor_service.domain.models import Execution, ExecutionStep
from executor_service.domain.ports import UnitOfWork
from executor_service.domain.results import ExecutionResultStore


class ExecutionCommandSupport:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        result_store: ExecutionResultStore,
        *,
        max_steps_per_operation: int,
        max_steps_per_execution: int,
    ) -> None:
        if max_steps_per_operation < 1:
            raise ValueError("max_steps_per_operation must be positive.")
        if max_steps_per_execution < max_steps_per_operation:
            raise ValueError(
                "max_steps_per_execution must be at least "
                "max_steps_per_operation."
            )
        self.uow_factory = uow_factory
        self._result_store = result_store
        self._max_steps_per_operation = max_steps_per_operation
        self._max_steps_per_execution = max_steps_per_execution

    def validate_step_limits(
        self, current_step_count: int, new_step_count: int
    ) -> None:
        if new_step_count > self._max_steps_per_operation:
            raise InvalidStateTransitionError(
                "Operation Step count exceeds the configured maximum of "
                f"{self._max_steps_per_operation}."
            )
        if current_step_count + new_step_count > self._max_steps_per_execution:
            raise InvalidStateTransitionError(
                "Execution Step count exceeds the configured maximum of "
                f"{self._max_steps_per_execution}."
            )

    async def snapshot_sources(
        self, steps: list[ExecutionStep], execution_id: UUID
    ) -> None:
        for step in steps:
            source = await self._result_store.snapshot_source(
                execution_id, step.id, step.code
            )
            step.source_snapshot_path = source.relative_path
            step.source_size_bytes = source.size_bytes
            if step.source_sha256 and (
                source.checksum_sha256 != step.source_sha256
            ):
                raise InvalidStateTransitionError(
                    "Resolved Step source checksum changed before persistence."
                )
            step.source_sha256 = source.checksum_sha256


def fingerprint(
    command: SubmitExecutionCommand
    | CreateOperationCommand
    | FinalizeExecutionCommand,
) -> str:
    payload = asdict(command)
    payload.pop("idempotency_key")
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def ensure_same_fingerprint(execution: Execution, value: str) -> None:
    if execution.request_fingerprint != value:
        raise IdempotencyConflictError(
            "The submit idempotency_key was already used with a different request."
        )


def validate_submit(command: SubmitExecutionCommand) -> None:
    if command.spec_schema_version != "1.0":
        raise InvalidStateTransitionError(
            "Only ExecutionSpec schema_version 1.0 is supported."
        )
    validate_actor(command.actor_type, command.actor_id)
    if (
        command.actor_type is not None
        and command.trigger_type == TriggerType.BATCH
        and command.actor_type != ActorType.BATCH
    ):
        raise InvalidStateTransitionError("BATCH submit requires BATCH actor.")
    if (
        command.actor_type is not None
        and command.trigger_type == TriggerType.INTERACTIVE
        and command.actor_type not in {ActorType.AGENT, ActorType.USER}
    ):
        raise InvalidStateTransitionError(
            "INTERACTIVE submit requires AGENT or USER actor."
        )
    if (
        command.actor_type == ActorType.USER
        and command.actor_id != command.user_id
    ):
        raise InvalidStateTransitionError(
            "INTERACTIVE submit requires actor.id to match context.user_id."
        )
    if command.session_id is not None and command.project_id is None:
        raise InvalidStateTransitionError("session_id requires project_id.")
    if command.project_id == "unscoped" or command.session_id == "unscoped":
        raise InvalidStateTransitionError(
            "'unscoped' is reserved for workspace paths."
        )
    if not command.steps:
        raise InvalidStateTransitionError(
            "ExecutionSpec must contain at least one step."
        )
    sequences = [step.sequence for step in command.steps]
    if sequences != list(range(len(command.steps))):
        raise InvalidStateTransitionError(
            "ExecutionSpec step sequences must be contiguous and start at 0."
        )
    if any(not step.code.strip() for step in command.steps):
        raise InvalidStateTransitionError(
            "ExecutionSpec step code must not be blank."
        )
    if command.operation_mode == OperationMode.SINGLE:
        if command.operation_wait_timeout_seconds is not None:
            raise InvalidStateTransitionError(
                "SINGLE execution does not accept "
                "operation_wait_timeout_seconds."
            )
        return
    if command.trigger_type != TriggerType.INTERACTIVE:
        raise InvalidStateTransitionError(
            "MULTI execution requires INTERACTIVE trigger_type."
        )
    if command.operation_wait_timeout_seconds is None:
        raise InvalidStateTransitionError(
            "MULTI execution requires operation_wait_timeout_seconds."
        )


def code_hash(code: str | None) -> str | None:
    return (
        hashlib.sha256(code.encode()).hexdigest() if code is not None else None
    )


def ensure_same_receipt(
    receipt: tuple[str, str, dict[str, object]],
    command_type: str,
    value: str,
) -> None:
    receipt_type, receipt_fingerprint, _ = receipt
    if receipt_type != command_type or receipt_fingerprint != value:
        raise IdempotencyConflictError(
            "idempotency_key was already used with a different command."
        )


def required_operation_id(operation_id: UUID | None) -> UUID:
    if operation_id is None:
        raise PersistenceConflictError(
            "Accepted command has no persisted Operation."
        )
    return operation_id


def operation_id_from_receipt(
    receipt: tuple[str, str, dict[str, object]],
) -> UUID:
    value = receipt[2].get("operation_id")
    if not isinstance(value, str):
        raise PersistenceConflictError(
            "Accepted continue command has no Operation receipt."
        )
    return UUID(value)


def validate_actor(actor_type: ActorType | None, actor_id: str | None) -> None:
    if (actor_type is None) != (actor_id is None):
        raise InvalidStateTransitionError(
            "actor_type and actor_id must be provided together."
        )
    if actor_id is not None and not actor_id.strip():
        raise InvalidStateTransitionError("actor_id must not be blank.")


def apply_actor(
    execution: Execution,
    actor_type: ActorType | None,
    actor_id: str | None,
) -> None:
    validate_actor(actor_type, actor_id)
    if actor_type is None:
        return
    execution.updated_by_type = actor_type
    execution.updated_by = actor_id


def apply_step_actor(
    step: ExecutionStep,
    actor_type: ActorType | None,
    actor_id: str | None,
) -> None:
    validate_actor(actor_type, actor_id)
    if actor_type is None:
        return
    step.updated_by_type = actor_type
    step.updated_by = actor_id


async def required_execution(uow: UnitOfWork, execution_id: UUID) -> Execution:
    execution = await uow.executions.get(execution_id)
    if execution is None:
        raise ExecutionNotFoundError(
            f"Execution {execution_id} was not found."
        )
    return execution
