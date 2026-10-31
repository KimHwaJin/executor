"""Shared validation and persistence support for Execution commands."""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from uuid import UUID

from executor_service.application.commands import (
    CreateOperationCommand,
    FinalizeExecutionCommand,
    StepSpec,
    SubmitExecutionCommand,
)
from executor_service.application.services.types import (
    UnitOfWorkFactory,
)
from executor_service.domain.enums import (
    ActorType,
    OperationMode,
    TriggerType,
)
from executor_service.domain.errors import (
    ExecutionNotFoundError,
    IdempotencyConflictError,
    InvalidExecutionSpecError,
    InvalidStateTransitionError,
    PersistenceConflictError,
)
from executor_service.domain.fingerprints import canonical_sha256
from executor_service.domain.models import Execution, ExecutionStep
from executor_service.domain.ports import UnitOfWork
from executor_service.domain.results import ExecutionResultStore

# Where a value sits in the request: "body" or "header", then field names.
Location = tuple[str | int, ...]
SUBMIT_OPERATION_LOCATION: Location = ("body", "operation")
OPERATION_CREATE_LOCATION: Location = ("body",)


class ExecutionCommandSupport:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        result_store: ExecutionResultStore,
        *,
        max_steps_per_operation: int,
        max_steps_per_execution: int,
        max_runtime_seconds: int = 604800,
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
        self._max_runtime_seconds = max_runtime_seconds

    def validate_timeouts(
        self,
        operation_timeout_seconds: int | None,
        step_timeout_seconds: Iterable[int | None],
        *,
        location: Location,
    ) -> None:
        """Reject limits the Execution's maximum runtime would cut first.

        ``location`` is where the Operation's fields sit in the request.
        """
        limit = self._max_runtime_seconds
        if operation_timeout_seconds is not None and (
            operation_timeout_seconds > limit
        ):
            raise InvalidExecutionSpecError.at(
                (*location, "operation_timeout_seconds"),
                f"operation_timeout_seconds ({operation_timeout_seconds}) "
                f"exceeds the Execution maximum runtime of {limit} seconds.",
                "exceeds_max_runtime",
                limit_seconds=limit,
                actual_seconds=operation_timeout_seconds,
            )
        for index, value in enumerate(step_timeout_seconds):
            if value is not None and value > limit:
                raise InvalidExecutionSpecError.at(
                    (
                        *location,
                        "spec",
                        "steps",
                        index,
                        "step_timeout_seconds",
                    ),
                    f"steps[{index}].step_timeout_seconds ({value}) exceeds "
                    f"the Execution maximum runtime of {limit} seconds.",
                    "exceeds_max_runtime",
                    limit_seconds=limit,
                    actual_seconds=value,
                )

    def validate_step_limits(
        self,
        current_step_count: int,
        new_step_count: int,
        *,
        location: Location,
    ) -> None:
        steps = (*location, "spec", "steps")
        if new_step_count > self._max_steps_per_operation:
            raise InvalidExecutionSpecError.at(
                steps,
                "An Operation may carry at most "
                f"{self._max_steps_per_operation} Steps; this one has "
                f"{new_step_count}.",
                "too_many_steps",
                limit=self._max_steps_per_operation,
                actual=new_step_count,
            )
        total = current_step_count + new_step_count
        if total > self._max_steps_per_execution:
            raise InvalidExecutionSpecError.at(
                steps,
                "An Execution may hold at most "
                f"{self._max_steps_per_execution} Steps; it has "
                f"{current_step_count} and this Operation adds "
                f"{new_step_count}.",
                "execution_step_limit_exceeded",
                limit=self._max_steps_per_execution,
                existing=current_step_count,
                requested=new_step_count,
                actual=total,
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
    return canonical_sha256(payload)


def ensure_same_fingerprint(execution: Execution, value: str) -> None:
    if execution.request_fingerprint != value:
        raise IdempotencyConflictError(
            "The submit idempotency_key was already used with a different request."
        )


def validate_submit(command: SubmitExecutionCommand) -> None:
    spec = (*SUBMIT_OPERATION_LOCATION, "spec")
    actor = ("body", "trigger", "actor")
    wait_timeout = ("body", "lifecycle", "operation_wait_timeout_seconds")
    validate_schema_version(command.spec_schema_version, spec)
    validate_actor(command.actor_type, command.actor_id, location=actor)
    allowed_actors = {
        TriggerType.BATCH: (ActorType.USER, ActorType.BATCH),
        TriggerType.INTERACTIVE: (ActorType.AGENT, ActorType.USER),
    }.get(command.trigger_type)
    if (
        command.actor_type is not None
        and allowed_actors is not None
        and command.actor_type not in allowed_actors
    ):
        allowed = " or ".join(allowed_actors)
        raise InvalidExecutionSpecError.at(
            (*actor, "type"),
            f"{command.trigger_type} submit requires a {allowed} actor, "
            f"not {command.actor_type}.",
            "actor_not_allowed_for_trigger",
            trigger_type=command.trigger_type,
            actual=command.actor_type,
            allowed=allowed,
        )
    if (
        command.actor_type == ActorType.USER
        and command.actor_id != command.user_id
    ):
        raise InvalidExecutionSpecError.at(
            (*actor, "id"),
            f"A USER actor.id ({command.actor_id}) must equal X-User-Id "
            f"({command.user_id}).",
            "actor_user_mismatch",
            actual=command.actor_id,
            expected=command.user_id,
        )
    if command.session_id is not None and command.project_id is None:
        raise InvalidExecutionSpecError.at(
            ("header", "X-Session-Id"),
            "X-Session-Id requires X-Project-Id.",
            "session_without_project",
        )
    for header, value in (
        ("X-Project-Id", command.project_id),
        ("X-Session-Id", command.session_id),
    ):
        if value == "unscoped":
            raise InvalidExecutionSpecError.at(
                ("header", header),
                f"'unscoped' is reserved for workspace paths; {header} "
                "must not use it.",
                "reserved_value",
                actual=value,
            )
    validate_operation_steps(command.steps, expected_start=0, location=spec)
    if command.operation_mode == OperationMode.SINGLE:
        if command.operation_wait_timeout_seconds is not None:
            raise InvalidExecutionSpecError.at(
                wait_timeout,
                "SINGLE execution does not accept "
                "operation_wait_timeout_seconds.",
                "not_allowed_for_single",
                actual=command.operation_wait_timeout_seconds,
            )
        return
    if command.trigger_type != TriggerType.INTERACTIVE:
        raise InvalidExecutionSpecError.at(
            ("body", "trigger", "type"),
            f"MULTI execution requires INTERACTIVE trigger, not "
            f"{command.trigger_type}.",
            "multi_requires_interactive",
            actual=command.trigger_type,
            expected=TriggerType.INTERACTIVE,
        )
    if command.operation_wait_timeout_seconds is None:
        raise InvalidExecutionSpecError.at(
            wait_timeout,
            "MULTI execution requires operation_wait_timeout_seconds.",
            "required_for_multi",
        )


def validate_schema_version(version: str, spec_location: Location) -> None:
    if version != "1.0":
        raise InvalidExecutionSpecError.at(
            (*spec_location, "schema_version"),
            f"Only ExecutionSpec schema_version 1.0 is supported, not "
            f"{version}.",
            "unsupported_schema_version",
            expected="1.0",
            actual=version,
        )


def validate_operation_steps(
    steps: Sequence[StepSpec], *, expected_start: int, location: Location
) -> None:
    """Steps present, numbered on from ``expected_start``, none blank."""
    steps_location = (*location, "steps")
    if not steps:
        raise InvalidExecutionSpecError.at(
            steps_location,
            "An Operation requires at least one Step.",
            "steps_empty",
        )
    for index, step in enumerate(steps):
        expected = expected_start + index
        if step.sequence != expected:
            raise InvalidExecutionSpecError.at(
                (*steps_location, index, "sequence"),
                f"Step sequences must be contiguous and start at "
                f"{expected_start}; steps[{index}] has {step.sequence}, "
                f"expected {expected}.",
                "sequence_not_contiguous",
                expected=expected,
                actual=step.sequence,
            )
        if not step.code.strip():
            raise InvalidExecutionSpecError.at(
                (*steps_location, index, "payload", "source"),
                f"Step {step.sequence} source must not be blank.",
                "source_blank",
                sequence=step.sequence,
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


def validate_actor(
    actor_type: ActorType | None,
    actor_id: str | None,
    *,
    location: Location = ("body", "actor"),
) -> None:
    if (actor_type is None) != (actor_id is None):
        raise InvalidExecutionSpecError.at(
            location,
            "actor.type and actor.id must be provided together.",
            "actor_incomplete",
        )
    if actor_id is not None and not actor_id.strip():
        raise InvalidExecutionSpecError.at(
            (*location, "id"), "actor.id must not be blank.", "actor_id_blank"
        )


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
