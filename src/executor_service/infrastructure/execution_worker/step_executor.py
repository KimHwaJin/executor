"""Execution Step state transitions and durable output delivery."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.diagnostics import DiagnosticCategory
from executor_service.domain.enums import ExecutionStatus, StepStatus
from executor_service.domain.models import (
    ExecutionStep,
    empty_output_summary,
    utc_now,
)
from executor_service.domain.results import (
    ExecutionResultStore,
    ExecutionSourceReference,
    StepResultDescriptor,
    StepResultIdentity,
)
from executor_service.domain.runtime import (
    RuntimeDriver,
    RuntimeDriverError,
    RuntimeExecutionError,
    RuntimeExecutionTimeoutError,
    RuntimeOutputLimitExceededError,
    RuntimeOutputRecord,
    RuntimeStreamingExecutor,
)
from executor_service.infrastructure.db.models import (
    ExecutionOperationORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
)
from executor_service.infrastructure.diagnostic_store import DiagnosticRecorder
from executor_service.infrastructure.execution_leases import (
    ExecutionLease,
    ExecutionLeaseLostError,
    require_active_lease,
)
from executor_service.infrastructure.execution_worker.event_writer import (
    add_start_events,
    add_step_completed_event,
    add_step_started_event,
)
from executor_service.infrastructure.execution_worker.failure_policy import (
    safe_error,
)
from executor_service.infrastructure.execution_worker.output_mapping import (
    output_record,
)
from executor_service.infrastructure.execution_worker.types import (
    StoredRuntimeExecutionError,
    StoredRuntimeExecutionTimeoutError,
    StoredRuntimeOutputLimitExceededError,
    StoredStepFailure,
)
from executor_service.infrastructure.runtime_diagnostics import (
    log_runtime_failure,
)

logger = logging.getLogger(__name__)


class ExecutionStepExecutor:
    """Executes a Step and persists its state and output references."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        result_store: ExecutionResultStore,
    ) -> None:
        self._session_factory = session_factory
        self._result_store = result_store
        self._diagnostics = DiagnosticRecorder(session_factory)

    async def ensure_count(
        self, lease: ExecutionLease, cell_count: int
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await require_active_lease(session, lease)
            steps = list(
                await session.scalars(
                    select(ExecutionStepORM)
                    .where(ExecutionStepORM.execution_id == lease.execution_id)
                    .order_by(ExecutionStepORM.sequence)
                )
            )
            if len(steps) != cell_count:
                raise ValueError(
                    f"Execution has {len(steps)} planned steps but source has "
                    f"{cell_count} cells."
                )

    async def record_runtime_session(
        self,
        lease: ExecutionLease,
        runtime_session_id: str,
        workspace_path: str,
        notebook_path: str,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            execution, attempt = await require_active_lease(session, lease)
            execution.runtime_session_id = runtime_session_id
            execution.workspace_path = workspace_path
            execution.notebook_path = notebook_path
            execution.updated_at = utc_now()
            attempt.runtime_session_id = runtime_session_id
            await add_start_events(session, lease.execution_id)

    async def mark_started(self, lease: ExecutionLease, sequence: int) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            await require_active_lease(session, lease)
            step = await session.scalar(
                select(ExecutionStepORM).where(
                    ExecutionStepORM.execution_id == lease.execution_id,
                    ExecutionStepORM.sequence == sequence,
                )
            )
            if step is None:
                raise ValueError(f"Execution Step {sequence} was not found.")
            step.status = StepStatus.RUNNING
            step.output_summary = empty_output_summary()
            step.result_execution_attempt_id = None
            step.result_manifest_path = None
            step.result_manifest_checksum_sha256 = None
            step.result_manifest_size_bytes = None
            step.result_fencing_token = None
            step.result_complete = None
            step.result_representation_count = 0
            step.result_total_size_bytes = 0
            step.started_at = now
            step.updated_at = now
            history = await session.scalar(
                select(ExecutionStepAttemptORM).where(
                    ExecutionStepAttemptORM.execution_attempt_id
                    == lease.attempt_id,
                    ExecutionStepAttemptORM.sequence == sequence,
                )
            )
            if history is None:
                session.add(
                    ExecutionStepAttemptORM(
                        execution_id=lease.execution_id,
                        execution_attempt_id=lease.attempt_id,
                        execution_step_id=step.id,
                        sequence=sequence,
                        skill_name=step.skill_name,
                        tool_name=step.tool_name,
                        input_parameters=step.input_parameters,
                        status=StepStatus.RUNNING,
                        output_summary=empty_output_summary(),
                        created_by_type=(
                            step.updated_by_type or step.created_by_type
                        ),
                        created_by=step.updated_by or step.created_by,
                        updated_by_type=(
                            step.updated_by_type or step.created_by_type
                        ),
                        updated_by=step.updated_by or step.created_by,
                        started_at=now,
                    )
                )
            else:
                history.status = StepStatus.RUNNING
                history.started_at = now
                history.finished_at = None
                history.error_message = None
                history.output_summary = empty_output_summary()
                history.result_manifest_path = None
                history.result_manifest_checksum_sha256 = None
                history.result_manifest_size_bytes = None
                history.result_fencing_token = None
                history.result_complete = None
                history.result_representation_count = 0
                history.result_total_size_bytes = 0
            if step.operation_id is None:
                raise ValueError(
                    f"Execution Step {sequence} has no Operation."
                )
            await add_step_started_event(session, lease, step)

    async def mark_succeeded(
        self,
        lease: ExecutionLease,
        sequence: int,
        stored_result: StepResultDescriptor,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            await require_active_lease(session, lease)
            step = await session.scalar(
                select(ExecutionStepORM).where(
                    ExecutionStepORM.execution_id == lease.execution_id,
                    ExecutionStepORM.sequence == sequence,
                )
            )
            if step is None or step.operation_id is None:
                raise ValueError(
                    f"Execution Step {sequence} or its Operation was not "
                    "found."
                )
            output_summary = stored_result.output_summary
            step.status = StepStatus.SUCCEEDED
            step.output_summary = output_summary
            step.result_execution_attempt_id = lease.attempt_id
            _assign_step_result(step, stored_result)
            step.finished_at = now
            step.updated_at = now
            await session.execute(
                update(ExecutionStepAttemptORM)
                .where(
                    ExecutionStepAttemptORM.execution_attempt_id
                    == lease.attempt_id,
                    ExecutionStepAttemptORM.sequence == sequence,
                )
                .values(
                    status=StepStatus.SUCCEEDED,
                    output_summary=output_summary,
                    result_manifest_path=stored_result.reference.relative_path,
                    result_manifest_checksum_sha256=(
                        stored_result.reference.checksum_sha256
                    ),
                    result_manifest_size_bytes=(
                        stored_result.reference.size_bytes
                    ),
                    result_fencing_token=stored_result.reference.fencing_token,
                    result_complete=stored_result.complete,
                    result_representation_count=(
                        stored_result.representation_count
                    ),
                    result_total_size_bytes=stored_result.total_size_bytes,
                    finished_at=now,
                )
            )
            await add_step_completed_event(
                session,
                lease,
                step,
                StepStatus.SUCCEEDED,
                stored_result=stored_result,
            )

    async def mark_failed(
        self,
        lease: ExecutionLease,
        sequence: int,
        stored_result: StepResultDescriptor,
        error_message: str,
        *,
        retryable: bool = True,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            await require_active_lease(session, lease)
            step = await session.scalar(
                select(ExecutionStepORM).where(
                    ExecutionStepORM.execution_id == lease.execution_id,
                    ExecutionStepORM.sequence == sequence,
                )
            )
            if step is None or step.operation_id is None:
                raise ValueError(
                    f"Execution Step {sequence} or its Operation was not "
                    "found."
                )
            safe_message = error_message[:2000]
            output_summary = stored_result.output_summary
            step.status = StepStatus.FAILED
            step.output_summary = output_summary
            step.result_execution_attempt_id = lease.attempt_id
            _assign_step_result(step, stored_result)
            step.error_message = safe_message
            step.finished_at = now
            step.updated_at = now
            await session.execute(
                update(ExecutionStepAttemptORM)
                .where(
                    ExecutionStepAttemptORM.execution_attempt_id
                    == lease.attempt_id,
                    ExecutionStepAttemptORM.sequence == sequence,
                )
                .values(
                    status=StepStatus.FAILED,
                    output_summary=output_summary,
                    result_manifest_path=stored_result.reference.relative_path,
                    result_manifest_checksum_sha256=(
                        stored_result.reference.checksum_sha256
                    ),
                    result_manifest_size_bytes=(
                        stored_result.reference.size_bytes
                    ),
                    result_fencing_token=stored_result.reference.fencing_token,
                    result_complete=stored_result.complete,
                    result_representation_count=(
                        stored_result.representation_count
                    ),
                    result_total_size_bytes=stored_result.total_size_bytes,
                    error_message=safe_message,
                    finished_at=now,
                )
            )
            await add_step_completed_event(
                session,
                lease,
                step,
                StepStatus.FAILED,
                stored_result=stored_result,
                error_message=safe_message,
                retryable=retryable,
            )

    async def execute(
        self,
        driver: RuntimeDriver,
        runtime_session_id: str,
        code: str,
        execution_id: UUID,
        sequence: int,
        *,
        result_identity: StepResultIdentity,
        source_reference: ExecutionSourceReference,
        lease: ExecutionLease,
    ) -> Any:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        ExecutionStepORM.step_timeout_seconds,
                        ExecutionOperationORM.operation_timeout_seconds,
                        ExecutionOperationORM.started_at,
                    )
                    .join(
                        ExecutionOperationORM,
                        ExecutionOperationORM.id
                        == ExecutionStepORM.operation_id,
                    )
                    .where(
                        ExecutionStepORM.execution_id == execution_id,
                        ExecutionStepORM.sequence == sequence,
                    )
                )
            ).one()
        timeouts: list[tuple[float, str]] = []
        if row.step_timeout_seconds is not None:
            timeouts.append((float(row.step_timeout_seconds), "Step"))
        if row.operation_timeout_seconds is not None:
            started_at = _as_utc(row.started_at or utc_now())
            remaining = (
                row.operation_timeout_seconds
                - (utc_now() - started_at).total_seconds()
            )
            if remaining <= 0:
                raise RuntimeExecutionTimeoutError(
                    "Operation", float(row.operation_timeout_seconds)
                )
            timeouts.append((remaining, "Operation"))
        observations: list[tuple[BaseException, str, datetime]] = []
        execution_call = self._execute_with_result_store(
            observations,
            driver,
            runtime_session_id,
            code,
            result_identity,
            source_reference,
        )
        try:
            if not timeouts:
                return await execution_call
            timeout_seconds, scope = min(timeouts)
            async with asyncio.timeout(timeout_seconds):
                return await execution_call
        except TimeoutError as exc:
            if not timeouts:
                raise
            failure = RuntimeExecutionTimeoutError(scope, timeout_seconds)
            self._log_failure(
                observations, result_identity, failure, "RUNTIME_TIMEOUT"
            )
            stored = await self._preserve_failure_result(
                observations, result_identity, failure
            )
            if stored is None:
                raise StoredStepFailure(failure, None) from exc
            raise StoredRuntimeExecutionTimeoutError(
                scope, timeout_seconds, stored
            ) from exc
        except asyncio.CancelledError as exc:
            stored = await self._preserve_failure_result(
                observations, result_identity, exc
            )
            if stored is not None:
                try:
                    async with asyncio.timeout(2):
                        await self._record_interrupted_result(
                            lease, sequence, stored
                        )
                except ExecutionLeaseLostError:
                    # The new owner alone may decide the terminal outcome.
                    # Never attach this file to a later Attempt or fence.
                    pass
                except Exception as persist_error:
                    self._log_failure(
                        observations,
                        result_identity,
                        persist_error,
                        "RESULT_REFERENCE_PERSIST",
                    )
            raise
        finally:
            # Diagnostic I/O is outside the execution deadline. A slow DB must
            # not transform a code/storage failure into a Step timeout.
            for error, phase, occurred_at in observations:
                await self._diagnostics.record(
                    lease,
                    error,
                    phase=phase,
                    sequence=result_identity.sequence,
                    occurred_at=occurred_at,
                    category=DiagnosticCategory.OUTPUT
                    if phase.startswith("RESULT_")
                    or isinstance(error, RuntimeOutputLimitExceededError)
                    else DiagnosticCategory.EXECUTION,
                )

    async def _record_interrupted_result(
        self,
        lease: ExecutionLease,
        sequence: int,
        stored: StepResultDescriptor,
    ) -> None:
        """Attach evidence before releasing ownership, without a transition."""

        async with self._session_factory() as session, session.begin():
            await require_active_lease(
                session,
                lease,
                allowed_statuses=(
                    ExecutionStatus.RUNNING,
                    ExecutionStatus.CANCEL_REQUESTED,
                ),
            )
            if (
                stored.reference.execution_attempt_id != lease.attempt_id
                or stored.reference.fencing_token != lease.fencing_token
            ):
                raise ValueError("Interrupted result ownership conflicts.")
            rows = (
                await session.execute(
                    select(ExecutionStepORM, ExecutionStepAttemptORM)
                    .join(
                        ExecutionStepAttemptORM,
                        ExecutionStepAttemptORM.execution_step_id
                        == ExecutionStepORM.id,
                    )
                    .where(
                        ExecutionStepORM.execution_id == lease.execution_id,
                        ExecutionStepORM.sequence == sequence,
                        ExecutionStepORM.status == StepStatus.RUNNING,
                        ExecutionStepAttemptORM.execution_attempt_id
                        == lease.attempt_id,
                        ExecutionStepAttemptORM.status == StepStatus.RUNNING,
                    )
                )
            ).one()
            step, history = rows
            step.result_execution_attempt_id = lease.attempt_id
            step.updated_at = utc_now()
            for row in (step, history):
                row.output_summary = stored.output_summary
                _assign_step_result(row, stored)

    @staticmethod
    def result_identity(
        step: ExecutionStep,
        lease: ExecutionLease,
    ) -> StepResultIdentity:
        if step.operation_id is None:
            raise ValueError(
                f"Execution Step {step.sequence} has no Operation."
            )
        return StepResultIdentity(
            execution_id=lease.execution_id,
            operation_id=step.operation_id,
            step_id=step.id,
            sequence=step.sequence,
            execution_attempt_id=lease.attempt_id,
            fencing_token=lease.fencing_token,
        )

    @staticmethod
    def source_reference(step: ExecutionStep) -> ExecutionSourceReference:
        if (
            step.source_snapshot_path is None
            or step.source_size_bytes is None
            or not step.source_sha256
        ):
            raise RuntimeDriverError(
                f"Execution Step {step.sequence} has no immutable source "
                "snapshot."
            )
        return ExecutionSourceReference(
            relative_path=step.source_snapshot_path,
            checksum_sha256=step.source_sha256,
            size_bytes=step.source_size_bytes,
        )

    async def _execute_with_result_store(
        self,
        observations: list[tuple[BaseException, str, datetime]],
        driver: RuntimeDriver,
        runtime_session_id: str,
        code: str,
        identity: StepResultIdentity,
        source_reference: ExecutionSourceReference,
    ) -> StepResultDescriptor:
        committed_offset = 0
        phase = "RESULT_PREPARE"

        async def append_output(record: RuntimeOutputRecord) -> None:
            nonlocal committed_offset, phase
            phase = "RESULT_APPEND"
            result = await self._result_store.append_step_outputs(
                identity,
                expected_offset=committed_offset,
                batch_id=uuid4(),
                records=(record,),
            )
            expected_committed_offset = committed_offset + 1
            if result.committed_offset != expected_committed_offset:
                raise RuntimeDriverError(
                    "Shared result append acknowledgement is invalid."
                )
            committed_offset = result.committed_offset
            phase = "RUNTIME_EXECUTE"

        streaming = isinstance(driver, RuntimeStreamingExecutor)
        try:
            await self._result_store.begin_step_result(
                identity, source_reference
            )
            phase = "RUNTIME_EXECUTE"
            if streaming:
                execution_result = await driver.execute_streaming(
                    runtime_session_id,
                    code,
                    append_output,
                )
            else:
                execution_result = await driver.execute(
                    runtime_session_id, code
                )
                for output in execution_result.outputs:
                    await append_output(output_record(output))
            phase = "RESULT_FINALIZE"
            return await self._result_store.finalize_step_result(
                identity,
                execution_count=execution_result.execution_count,
            )
        except RuntimeOutputLimitExceededError as exc:
            self._log_failure(observations, identity, exc, phase)
            if not streaming:
                try:
                    for output in exc.outputs:
                        await append_output(output_record(output))
                except Exception as storage_error:
                    self._log_failure(
                        observations,
                        identity,
                        storage_error,
                        "RESULT_FAILURE_SAVE",
                    )
            stored = await self._preserve_failure_result(
                observations, identity, exc
            )
            if stored is None:
                raise StoredStepFailure(exc, None) from exc
            raise StoredRuntimeOutputLimitExceededError(
                exc,
                stored,
            ) from exc
        except RuntimeExecutionError as exc:
            self._log_failure(observations, identity, exc, phase)
            try:
                if not streaming:
                    for output in exc.outputs:
                        await append_output(output_record(output))
                stored = await self._result_store.finalize_step_result(
                    identity,
                    execution_count=None,
                    error_message=safe_error(exc),
                )
            except Exception as storage_error:
                self._log_failure(
                    observations,
                    identity,
                    storage_error,
                    "RESULT_FAILURE_SAVE",
                )
                stored = await self._preserve_failure_result(
                    observations, identity, storage_error
                )
                raise StoredStepFailure(exc, stored) from exc
            raise StoredRuntimeExecutionError(
                str(exc), exc.outputs, stored
            ) from exc
        except Exception as exc:
            self._log_failure(observations, identity, exc, phase)
            stored = await self._preserve_failure_result(
                observations, identity, exc
            )
            raise StoredStepFailure(exc, stored) from exc

    async def _preserve_failure_result(
        self,
        observations: list[tuple[BaseException, str, datetime]],
        identity: StepResultIdentity,
        error: BaseException,
    ) -> StepResultDescriptor | None:
        try:
            return await self._result_store.abort_step_result(
                identity,
                reason=(
                    "Runtime execution was cancelled before output delivery completed."
                    if isinstance(error, asyncio.CancelledError)
                    else safe_error(error)
                    if isinstance(error, Exception)
                    else type(error).__name__
                ),
            )
        except Exception as storage_error:
            # A full/unwritable filesystem cannot save a manifest. Preserve the
            # initiating error and log the secondary failure independently.
            self._log_failure(
                observations, identity, storage_error, "RESULT_FAILURE_SAVE"
            )
            return None

    def _log_failure(
        self,
        observations: list[tuple[BaseException, str, datetime]],
        identity: StepResultIdentity,
        error: BaseException,
        phase: str,
    ) -> None:
        log_runtime_failure(
            logger,
            error,
            phase=phase,
            execution_id=identity.execution_id,
            operation_id=identity.operation_id,
            step_id=identity.step_id,
            sequence=identity.sequence,
            attempt_id=identity.execution_attempt_id,
            fencing_token=identity.fencing_token,
        )
        observations.append((error, phase, utc_now()))


def _assign_step_result(
    step: ExecutionStepORM | ExecutionStepAttemptORM,
    stored_result: StepResultDescriptor,
) -> None:
    step.result_manifest_path = stored_result.reference.relative_path
    step.result_manifest_checksum_sha256 = (
        stored_result.reference.checksum_sha256
    )
    step.result_manifest_size_bytes = stored_result.reference.size_bytes
    step.result_fencing_token = stored_result.reference.fencing_token
    step.result_complete = stored_result.complete
    step.result_representation_count = stored_result.representation_count
    step.result_total_size_bytes = stored_result.total_size_bytes


def _as_utc(value: datetime) -> datetime:
    """SQLite tests may return timezone-naive values for aware columns."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
