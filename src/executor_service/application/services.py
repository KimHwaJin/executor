"""Execution lifecycle use cases."""

import hashlib
import json
from collections.abc import Callable, Mapping
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
    ActorType,
    ExecutionMode,
    ExecutionStatus,
    RuntimePool,
    RuntimeType,
    TriggerType,
)
from executor_service.domain.errors import (
    ExecutionNotFoundError,
    IdempotencyConflictError,
    InvalidStateTransitionError,
    PersistenceConflictError,
    UnsupportedRuntimeProfileError,
)
from executor_service.domain.models import Execution, ExecutionStep
from executor_service.domain.ports import UnitOfWork
from executor_service.events import build_execution_event
from executor_service.tracing import capture_trace_carrier

UnitOfWorkFactory = Callable[[], UnitOfWork]


class ExecutionService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        runtime_profiles: Mapping[RuntimeType, tuple[str, ...]],
    ) -> None:
        self._uow_factory = uow_factory
        self._runtime_profiles = {
            runtime_type: tuple(profiles) for runtime_type, profiles in runtime_profiles.items()
        }

    @property
    def runtime_profiles(self) -> dict[str, tuple[str, ...]]:
        return {
            runtime_type.value: profiles
            for runtime_type, profiles in self._runtime_profiles.items()
        }

    async def submit(self, command: SubmitExecutionCommand) -> Execution:
        _validate_submit(command)
        allowed_profiles = self._runtime_profiles.get(command.runtime_type, ())
        if command.runtime_profile not in allowed_profiles:
            raise UnsupportedRuntimeProfileError(
                f"runtime_profile '{command.runtime_profile}' is not supported for "
                f"runtime_type '{command.runtime_type.value}'."
            )
        fingerprint = _fingerprint(command)
        trace_carrier = capture_trace_carrier()
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
                    runtime_type=command.runtime_type,
                    runtime_pool=RuntimePool(command.trigger_type.value),
                    runtime_profile=command.runtime_profile,
                    code_source_type=command.code_source_type,
                    source_content=command.source_content,
                    code_path=command.code_path,
                    source_sha256=command.source_sha256,
                    user_id=command.user_id,
                    project_id=command.project_id,
                    session_id=command.session_id,
                    task_id=command.task_id,
                    execution_plan_id=command.execution_plan_id,
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
                            code_hash=_code_hash(step.code),
                            execution_plan_id=step.execution_plan_id,
                            plan_step_id=step.plan_step_id,
                            skill_name=step.skill_name,
                            tool_name=step.tool_name,
                            input_parameters=step.input_parameters,
                            created_by_type=command.actor_type,
                            created_by=command.actor_id,
                            updated_by_type=command.actor_type,
                            updated_by=command.actor_id,
                        )
                        for step in command.steps
                    ],
                    traceparent=trace_carrier.traceparent,
                    tracestate=trace_carrier.tracestate,
                )
                await uow.executions.add(execution)
                await uow.outbox.add(
                    build_execution_event(
                        execution_id=execution.id,
                        event_type="execution.submitted",
                        payload={
                            "task_id": execution.task_id,
                            "execution_plan_id": execution.execution_plan_id,
                            "status": execution.status.value,
                        },
                        actor_type=command.actor_type,
                        actor_id=command.actor_id,
                        traceparent=execution.traceparent,
                        tracestate=execution.tracestate,
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
                    raise ExecutionNotFoundError(f"Execution {command.execution_id} was not found.")
                expected_sequence = len(execution.steps)
                if command.step.sequence != expected_sequence:
                    raise InvalidStateTransitionError(
                        f"Next dynamic step sequence must be {expected_sequence}."
                    )
                if not command.step.code or not command.step.code.strip():
                    raise InvalidStateTransitionError("Dynamic step code must not be empty.")
                execution.request_dynamic_continue(command.expected_version)
                _apply_actor(execution, command.actor_type, command.actor_id)
                _apply_current_trace(execution)
                step = ExecutionStep(
                    sequence=command.step.sequence,
                    code=command.step.code,
                    code_hash=_code_hash(command.step.code),
                    execution_plan_id=command.step.execution_plan_id,
                    plan_step_id=command.step.plan_step_id,
                    skill_name=command.step.skill_name,
                    tool_name=command.step.tool_name,
                    input_parameters=command.step.input_parameters,
                    created_by_type=command.actor_type,
                    created_by=command.actor_id,
                    updated_by_type=command.actor_type,
                    updated_by=command.actor_id,
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
                    build_execution_event(
                        execution_id=execution.id,
                        event_type="execution.continue_requested",
                        payload={
                            "task_id": execution.task_id,
                            "execution_plan_id": step.execution_plan_id,
                            "plan_step_id": step.plan_step_id,
                            "status": execution.status.value,
                            "sequence": step.sequence,
                            "version": execution.version,
                        },
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
                    raise ExecutionNotFoundError(f"Execution {command.execution_id} was not found.")
                execution.request_dynamic_finish(command.expected_version)
                _apply_actor(execution, command.actor_type, command.actor_id)
                _apply_current_trace(execution)
                await uow.executions.save(execution)
                await uow.executions.add_command_receipt(
                    command.idempotency_key,
                    command_type,
                    fingerprint,
                    {"execution_id": str(execution.id)},
                )
                await uow.outbox.add(
                    build_execution_event(
                        execution_id=execution.id,
                        event_type="execution.finish_requested",
                        payload={
                            "task_id": execution.task_id,
                            "execution_plan_id": execution.execution_plan_id,
                            "status": execution.status.value,
                            "version": execution.version,
                        },
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
                _apply_actor(execution, command.actor_type, command.actor_id)
                _apply_current_trace(execution)
                await uow.executions.save(execution)
                await uow.outbox.add(
                    build_execution_event(
                        execution_id=execution.id,
                        event_type="execution.cancel_requested",
                        payload={
                            "task_id": execution.task_id,
                            "execution_plan_id": execution.execution_plan_id,
                            "status": execution.status.value,
                        },
                        actor_type=command.actor_type,
                        actor_id=command.actor_id,
                        traceparent=execution.traceparent,
                        tracestate=execution.tracestate,
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
                    raise ExecutionNotFoundError(f"Execution {command.execution_id} was not found.")
                execution.request_retry()
                _apply_actor(execution, command.actor_type, command.actor_id)
                for step in execution.steps:
                    if (
                        execution.retry_from_sequence is not None
                        and step.sequence >= execution.retry_from_sequence
                    ):
                        _apply_step_actor(step, command.actor_type, command.actor_id)
                _apply_current_trace(execution)
                if execution.retry_from_sequence is None:
                    raise RuntimeError("Retry sequence unexpectedly missing.")
                await uow.executions.save(execution)
                await uow.executions.add_retry_receipt(
                    execution.id,
                    command.idempotency_key,
                    execution.retry_from_sequence,
                )
                await uow.outbox.add(
                    build_execution_event(
                        execution_id=execution.id,
                        event_type="execution.retry_requested",
                        payload={
                            "task_id": execution.task_id,
                            "execution_plan_id": execution.execution_plan_id,
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
                        actor_type=command.actor_type,
                        actor_id=command.actor_id,
                        traceparent=execution.traceparent,
                        tracestate=execution.tracestate,
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
    _validate_actor(command.actor_type, command.actor_id)
    expected_actor_type = (
        ActorType.BATCH if command.trigger_type == TriggerType.BATCH else ActorType.USER
    )
    if command.actor_type is not None and command.actor_type != expected_actor_type:
        raise InvalidStateTransitionError(
            f"{command.trigger_type.value} submit requires {expected_actor_type.value} actor."
        )
    if (
        command.trigger_type == TriggerType.INTERACTIVE
        and command.actor_type == ActorType.USER
        and command.actor_id != command.user_id
    ):
        raise InvalidStateTransitionError(
            "INTERACTIVE submit requires actor.id to match context.user_id."
        )
    if command.trigger_type == TriggerType.BATCH and not command.workflow_id:
        raise InvalidStateTransitionError("BATCH submit requires context.workflow_id.")
    if not command.steps:
        raise InvalidStateTransitionError("ExecutionSpec must contain at least one step.")
    sequences = [step.sequence for step in command.steps]
    if sequences != list(range(len(command.steps))):
        raise InvalidStateTransitionError(
            "ExecutionSpec step sequences must be contiguous and start at 0."
        )
    plan_step_ids = [step.plan_step_id for step in command.steps]
    if len(plan_step_ids) != len(set(plan_step_ids)):
        raise InvalidStateTransitionError("ExecutionSpec plan_step_id values must be unique.")
    if any(not step.code.strip() for step in command.steps):
        raise InvalidStateTransitionError("ExecutionSpec step code must not be blank.")
    if any(step.execution_plan_id != command.execution_plan_id for step in command.steps):
        raise InvalidStateTransitionError(
            "Submit steps must belong to the submitted execution_plan_id."
        )
    if command.mode != ExecutionMode.DYNAMIC:
        return
    if command.trigger_type != TriggerType.INTERACTIVE:
        raise InvalidStateTransitionError("DYNAMIC execution requires INTERACTIVE trigger_type.")
    if len(command.steps) != 1 or command.steps[0].sequence != 0:
        raise InvalidStateTransitionError(
            "DYNAMIC submit requires exactly the first step (sequence 0)."
        )


def _code_hash(code: str | None) -> str | None:
    return hashlib.sha256(code.encode()).hexdigest() if code is not None else None


def _ensure_same_receipt(
    receipt: tuple[str, str, dict[str, object]], command_type: str, fingerprint: str
) -> None:
    receipt_type, receipt_fingerprint, _ = receipt
    if receipt_type != command_type or receipt_fingerprint != fingerprint:
        raise IdempotencyConflictError("idempotency_key was already used with a different command.")


def _validate_actor(actor_type: ActorType | None, actor_id: str | None) -> None:
    if (actor_type is None) != (actor_id is None):
        raise InvalidStateTransitionError("actor_type and actor_id must be provided together.")
    if actor_id is not None and not actor_id.strip():
        raise InvalidStateTransitionError("actor_id must not be blank.")


def _apply_actor(execution: Execution, actor_type: ActorType | None, actor_id: str | None) -> None:
    _validate_actor(actor_type, actor_id)
    if actor_type is None:
        return
    execution.updated_by_type = actor_type
    execution.updated_by = actor_id


def _apply_step_actor(
    step: ExecutionStep, actor_type: ActorType | None, actor_id: str | None
) -> None:
    _validate_actor(actor_type, actor_id)
    if actor_type is None:
        return
    step.updated_by_type = actor_type
    step.updated_by = actor_id


async def _required_execution(uow: UnitOfWork, execution_id: UUID) -> Execution:
    execution = await uow.executions.get(execution_id)
    if execution is None:
        raise ExecutionNotFoundError(f"Execution {execution_id} was not found.")
    return execution


def _apply_current_trace(execution: Execution) -> None:
    carrier = capture_trace_carrier()
    if carrier.traceparent is not None:
        execution.traceparent = carrier.traceparent
        execution.tracestate = carrier.tracestate
