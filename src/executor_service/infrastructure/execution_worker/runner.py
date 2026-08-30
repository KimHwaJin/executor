"""Single and MULTI Runtime execution orchestration."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

from opentelemetry.trace import SpanKind
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.config import Settings
from executor_service.domain.enums import (
    ArtifactStatus,
    AttemptStatus,
    ExecutionStatus,
    FailureType,
    OperationMode,
    OperationStatus,
    RetryStrategy,
    RuntimeAbortStatus,
    RuntimePool,
    RuntimeSessionCleanupStatus,
    StepStatus,
)
from executor_service.domain.models import Execution, utc_now
from executor_service.domain.results import ExecutionResultStore
from executor_service.domain.runtime import (
    RuntimeAbortResult,
    RuntimeDriver,
    RuntimeDriverError,
    RuntimeDriverFactory,
    RuntimeExecutionError,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionOperationORM,
    ExecutionORM,
    ExecutionStepAttemptORM,
    ExecutionStepORM,
    RuntimeTargetORM,
)
from executor_service.infrastructure.execution_leases import (
    ExecutionLease,
    ExecutionLeaseLostError,
    require_active_lease,
)
from executor_service.infrastructure.execution_worker.claiming import (
    ExecutionClaimer,
)
from executor_service.infrastructure.execution_worker.event_writer import (
    add_execution_completed_event,
    add_operation_completed_event,
    add_step_history_completed_event,
)
from executor_service.infrastructure.execution_worker.failure_policy import (
    failure_policy as _failure_policy,
)
from executor_service.infrastructure.execution_worker.failure_policy import (
    safe_error as _safe_error,
)
from executor_service.infrastructure.execution_worker.lease_heartbeat import (
    LeaseHeartbeatManager,
)
from executor_service.infrastructure.execution_worker.notebook_projector import (
    NotebookProjector,
)
from executor_service.infrastructure.execution_worker.runtime_cleanup import (
    best_effort_session_stop,
)
from executor_service.infrastructure.execution_worker.step_executor import (
    ExecutionStepExecutor,
)
from executor_service.infrastructure.execution_worker.types import (
    RetainedRuntimeSessionLostError,
    RuntimeAbortResolution,
    StoredRuntimeExecutionError,
    StoredRuntimeExecutionTimeoutError,
    StoredRuntimeOutputLimitExceededError,
)
from executor_service.infrastructure.runtime_registry import (
    RuntimeTargetRegistry,
)
from executor_service.infrastructure.workspace import WorkspaceManager
from executor_service.tracing import TracingManager

logger = logging.getLogger(__name__)


class ExecutionRunner:
    """Runs claimed Execution work against a Runtime Driver."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        registry: RuntimeTargetRegistry,
        driver_factory: RuntimeDriverFactory,
        artifacts: ExecutionArtifactManager,
        result_store: ExecutionResultStore,
        workspace: WorkspaceManager,
        claimer: ExecutionClaimer,
        lease_heartbeat: LeaseHeartbeatManager,
        notebook_projector: NotebookProjector,
        step_executor: ExecutionStepExecutor,
        tracing: TracingManager,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._registry = registry
        self._driver_factory = driver_factory
        self._artifacts = artifacts
        self._result_store = result_store
        self._workspace = workspace
        self._claimer = claimer
        self._lease_heartbeat = lease_heartbeat
        self._notebook_projector = notebook_projector
        self._step_executor = step_executor
        self._tracing = tracing

    def _create_driver(self, target: RuntimeTargetORM) -> RuntimeDriver:
        credential = self._registry.resolve_credential(
            target.credential_ref, target.credential_ciphertext
        )
        return self._driver_factory.create(
            target.runtime_type, target.connection_config, credential
        )

    async def run(self, execution_id: UUID) -> None:
        pool = await self._execution_pool(execution_id)
        if pool is None:
            return
        with self._tracing.span(
            "executor.worker.execution",
            kind=SpanKind.CONSUMER,
            attributes={
                "executor.execution.id": str(execution_id),
                "executor.runtime.pool": pool.value,
            },
        ):
            await self._run_execution_impl(execution_id, pool)

    async def _execution_pool(self, execution_id: UUID) -> RuntimePool | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(ExecutionORM.runtime_pool).where(
                    ExecutionORM.id == execution_id
                )
            )

    @asynccontextmanager
    async def _pool_activity(self, pool: RuntimePool) -> AsyncIterator[None]:
        del pool
        yield

    async def _trace_runtime[T](
        self,
        name: str,
        operation: Awaitable[T],
        *,
        execution_id: UUID,
        target_id: UUID,
        sequence: int | None = None,
    ) -> T:
        attributes: dict[str, object] = {
            "executor.execution.id": str(execution_id),
            "executor.runtime.target.id": str(target_id),
        }
        if sequence is not None:
            attributes["executor.step.sequence"] = sequence
        with self._tracing.span(name, attributes=attributes):
            return await operation

    async def _run_execution_impl(
        self, execution_id: UUID, pool: RuntimePool
    ) -> None:
        async with self._pool_activity(pool):
            claimed = await self._claimer.claim(execution_id)
            if claimed is None:
                return
            execution, target, lease = claimed
            if execution.operation_mode == OperationMode.MULTI:
                await self._run_multi_execution(execution, target, lease)
                return
            driver = self._create_driver(target)
            runtime_session_id: str | None = None
            heartbeat = asyncio.create_task(
                self._lease_heartbeat.run_execution(lease),
                name=f"heartbeat-{execution.id}",
            )
            failed_sequence: int | None = None
            try:
                resume = (
                    execution.retry_count > 0
                    and execution.retry_strategy
                    == RetryStrategy.FROM_FAILED_STEP
                    and execution.retry_from_sequence is not None
                    and execution.runtime_session_id is not None
                )
                if resume:
                    runtime_session_id = execution.runtime_session_id
                    try:
                        session_exists = await self._trace_runtime(
                            "executor.runtime.session.exists",
                            driver.session_exists(runtime_session_id),
                            execution_id=execution.id,
                            target_id=target.id,
                        )
                    except RuntimeDriverError as exc:
                        await self._claimer.defer_retained_retry(
                            lease,
                            target.id,
                            f"{type(exc).__name__}: retained runtime session preflight failed",
                        )
                        return
                    if not session_exists:
                        raise RetainedRuntimeSessionLostError(
                            "The retained Runtime session no longer exists."
                        )
                workspace = self._workspace.plan(execution)
                await self._trace_runtime(
                    "executor.runtime.workspace.prepare",
                    driver.prepare_workspace(workspace.runtime_relative_path),
                    execution_id=execution.id,
                    target_id=target.id,
                )
                steps_by_sequence = {
                    step.sequence: step for step in execution.steps
                }
                source_references = {
                    sequence: self._step_executor.source_reference(step)
                    for sequence, step in steps_by_sequence.items()
                }
                cells = [
                    await self._result_store.read_source(
                        source_references[sequence]
                    )
                    for sequence in range(len(steps_by_sequence))
                ]
                await self._step_executor.ensure_count(lease, len(cells))
                await self._notebook_projector.prepare(
                    driver,
                    lease,
                    execution.id,
                    execution.runtime_profile,
                    workspace,
                    execution.steps,
                    target.id,
                    source_references,
                )
                start_sequence = execution.retry_from_sequence if resume else 0
                if not resume:
                    runtime_session_id = await self._trace_runtime(
                        "executor.runtime.session.start",
                        driver.start_session(
                            execution.runtime_profile,
                            workspace.runtime_relative_path,
                        ),
                        execution_id=execution.id,
                        target_id=target.id,
                    )
                if runtime_session_id is None:
                    raise RuntimeError(
                        "Runtime session ID was not established."
                    )
                await self._step_executor.record_runtime_session(
                    lease,
                    runtime_session_id,
                    workspace.runtime_relative_path,
                    workspace.notebook_path,
                )
                for sequence in range(start_sequence, len(cells)):
                    code = cells[sequence]
                    artifact_snapshot = await self._artifacts.snapshot(
                        driver, workspace
                    )
                    await self._step_executor.mark_started(lease, sequence)
                    try:
                        result = await self._trace_runtime(
                            "executor.runtime.code.execute",
                            self._step_executor.execute(
                                driver,
                                runtime_session_id,
                                code,
                                execution.id,
                                sequence,
                                result_identity=self._step_executor.result_identity(
                                    steps_by_sequence[sequence], lease
                                ),
                                source_reference=source_references[sequence],
                            ),
                            execution_id=execution.id,
                            target_id=target.id,
                            sequence=sequence,
                        )
                    except asyncio.CancelledError:
                        # A user cancellation or Worker drain can interrupt a cell after it has
                        # written files. Preserve those files as incomplete execution evidence
                        # before the outer cancellation path tears down the Runtime session.
                        try:
                            await self._artifacts.discover_and_register(
                                driver=driver,
                                workspace=workspace,
                                before=artifact_snapshot,
                                lease=lease,
                                sequence=sequence,
                                status=ArtifactStatus.INCOMPLETE,
                                allow_cancel_requested=True,
                            )
                        except Exception as artifact_exc:
                            await self._record_artifact_failure(
                                lease,
                                sequence,
                                artifact_exc,
                            )
                            logger.warning(
                                "Cancelled-cell Artifact registration failed",
                                extra={"execution_id": str(execution.id)},
                            )
                        raise
                    except (
                        StoredRuntimeExecutionTimeoutError,
                        StoredRuntimeOutputLimitExceededError,
                    ):
                        failed_sequence = sequence
                        raise
                    except StoredRuntimeExecutionError as exc:
                        failed_sequence = sequence
                        await self._step_executor.mark_failed(
                            lease,
                            sequence,
                            exc.stored_result,
                            str(exc),
                        )
                        await self._notebook_projector.project(
                            driver,
                            lease,
                            execution.runtime_profile,
                            workspace,
                        )
                        try:
                            await self._artifacts.discover_and_register(
                                driver=driver,
                                workspace=workspace,
                                before=artifact_snapshot,
                                lease=lease,
                                sequence=sequence,
                                status=ArtifactStatus.INCOMPLETE,
                            )
                        except Exception as artifact_exc:
                            await self._record_artifact_failure(
                                lease,
                                sequence,
                                artifact_exc,
                            )
                            logger.warning(
                                "Incomplete Artifact registration failed",
                                extra={"execution_id": str(execution.id)},
                            )
                        raise
                    await self._step_executor.mark_succeeded(
                        lease,
                        sequence,
                        result,
                    )
                    await self._notebook_projector.project(
                        driver,
                        lease,
                        execution.runtime_profile,
                        workspace,
                    )
                    try:
                        await self._artifacts.discover_and_register(
                            driver=driver,
                            workspace=workspace,
                            before=artifact_snapshot,
                            lease=lease,
                            sequence=sequence,
                            status=ArtifactStatus.AVAILABLE,
                        )
                    except Exception as artifact_exc:
                        await self._record_artifact_failure(
                            lease,
                            sequence,
                            artifact_exc,
                        )
                        raise
                await self._notebook_projector.register_artifact(
                    driver=driver,
                    workspace=workspace,
                    lease=lease,
                    sequence=len(cells) - 1,
                )
                await self._lease_heartbeat.assert_execution(lease)
                await self._trace_runtime(
                    "executor.runtime.session.delete",
                    driver.delete_session(runtime_session_id),
                    execution_id=execution.id,
                    target_id=target.id,
                )
                await self.finalize(
                    lease,
                    ExecutionStatus.SUCCEEDED,
                    runtime_session_cleanup_status=RuntimeSessionCleanupStatus.SUCCEEDED,
                )
            except asyncio.CancelledError:
                if await self._cancellation_job_owns_terminal(execution.id):
                    # The replacement cancellation job exclusively owns Runtime cleanup and the
                    # CANCELLED transition. This execution job only preserves cell evidence.
                    await self.release_for_cancellation(lease)
                    raise
                cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
                if runtime_session_id is not None:
                    try:
                        async with asyncio.timeout(
                            self._settings.execution_shutdown_cleanup_seconds
                        ):
                            cleanup_status = await best_effort_session_stop(
                                driver, runtime_session_id
                            )
                    except TimeoutError:
                        cleanup_status = RuntimeSessionCleanupStatus.FAILED
                        logger.warning(
                            "Runtime session cleanup exceeded the Worker shutdown deadline",
                            extra={"execution_id": str(execution.id)},
                        )
                await self.finalize(
                    lease,
                    ExecutionStatus.FAILED,
                    "Executor worker stopped while the execution was running.",
                    failure_type=FailureType.WORKER_SHUTDOWN,
                    retry_strategy=RetryStrategy.FROM_START,
                    runtime_session_cleanup_status=cleanup_status,
                )
                raise
            except (
                StoredRuntimeExecutionTimeoutError,
                StoredRuntimeOutputLimitExceededError,
            ) as exc:
                if runtime_session_id is None or failed_sequence is None:
                    raise RuntimeError(
                        "Runtime abort workflow has no active session or Step."
                    ) from exc
                failure_type = (
                    FailureType.OUTPUT_LIMIT_EXCEEDED
                    if isinstance(exc, StoredRuntimeOutputLimitExceededError)
                    else (
                        FailureType.STEP_TIMEOUT
                        if exc.scope == "Step"
                        else FailureType.OPERATION_TIMEOUT
                    )
                )
                resolution = await self._resolve_runtime_abort(
                    lease,
                    driver,
                    runtime_session_id,
                    failure_type,
                )
                await self._step_executor.mark_failed(
                    lease,
                    failed_sequence,
                    exc.stored_result,
                    str(exc),
                )
                await self._notebook_projector.project(
                    driver,
                    lease,
                    execution.runtime_profile,
                    workspace,
                )
                try:
                    await self._artifacts.discover_and_register(
                        driver=driver,
                        workspace=workspace,
                        before=artifact_snapshot,
                        lease=lease,
                        sequence=failed_sequence,
                        status=ArtifactStatus.INCOMPLETE,
                    )
                except Exception as artifact_exc:
                    await self._record_artifact_failure(
                        lease,
                        failed_sequence,
                        artifact_exc,
                    )
                    logger.warning(
                        "Aborted-step Artifact registration failed",
                        extra={"execution_id": str(execution.id)},
                    )
                await self.finalize(
                    lease,
                    ExecutionStatus.FAILED,
                    str(exc),
                    retain_session=resolution.retain_session,
                    retry_from_sequence=failed_sequence,
                    failure_type=failure_type,
                    retry_strategy=resolution.retry_strategy,
                    runtime_session_cleanup_status=resolution.cleanup_status,
                )
            except ExecutionLeaseLostError:
                logger.warning(
                    "Execution Worker lost its lease fence; stale results "
                    "were discarded",
                    extra={
                        "execution_id": str(lease.execution_id),
                        "execution_attempt_id": str(lease.attempt_id),
                        "fencing_token": lease.fencing_token,
                    },
                )
            except Exception as exc:
                retain_session = (
                    isinstance(exc, RuntimeExecutionError)
                    and runtime_session_id is not None
                    and failed_sequence is not None
                )
                failure_type, retry_strategy = _failure_policy(
                    exc, retain_session
                )
                cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
                if runtime_session_id is not None and not retain_session:
                    cleanup_status = await best_effort_session_stop(
                        driver, runtime_session_id
                    )
                await self.finalize(
                    lease,
                    ExecutionStatus.FAILED,
                    _safe_error(exc),
                    retain_session=retain_session,
                    retry_from_sequence=failed_sequence,
                    failure_type=failure_type,
                    retry_strategy=retry_strategy,
                    runtime_session_cleanup_status=cleanup_status,
                )
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                await driver.close()

    async def _run_multi_execution(
        self,
        execution: Execution,
        target: RuntimeTargetORM,
        lease: ExecutionLease,
    ) -> None:
        driver = self._create_driver(target)
        heartbeat = asyncio.create_task(
            self._lease_heartbeat.run_execution(lease),
            name=f"heartbeat-{execution.id}",
        )
        runtime_session_id = execution.runtime_session_id
        try:
            workspace = self._workspace.plan(execution)
            await self._trace_runtime(
                "executor.runtime.workspace.prepare",
                driver.prepare_workspace(workspace.runtime_relative_path),
                execution_id=execution.id,
                target_id=target.id,
            )
            if runtime_session_id is None:
                runtime_session_id = await self._trace_runtime(
                    "executor.runtime.session.start",
                    driver.start_session(
                        execution.runtime_profile,
                        workspace.runtime_relative_path,
                    ),
                    execution_id=execution.id,
                    target_id=target.id,
                )
            await self._step_executor.record_runtime_session(
                lease,
                runtime_session_id,
                workspace.runtime_relative_path,
                workspace.notebook_path,
            )
            if execution.finalization_requested:
                last_sequence = max(
                    (step.sequence for step in execution.steps), default=0
                )
                await self._notebook_projector.register_artifact(
                    driver=driver,
                    workspace=workspace,
                    lease=lease,
                    sequence=last_sequence,
                )
                await self._lease_heartbeat.assert_execution(lease)
                await self._trace_runtime(
                    "executor.runtime.session.delete",
                    driver.delete_session(runtime_session_id),
                    execution_id=execution.id,
                    target_id=target.id,
                )
                await self.finalize(
                    lease,
                    ExecutionStatus.SUCCEEDED,
                    runtime_session_cleanup_status=RuntimeSessionCleanupStatus.SUCCEEDED,
                )
                return

            operation_id = execution.active_operation_id
            if operation_id is None:
                raise ValueError(
                    "Queued MULTI execution has no active Operation."
                )
            pending_steps = [
                step
                for step in execution.steps
                if step.operation_id == operation_id
                and step.status == StepStatus.PENDING
            ]
            if not pending_steps:
                raise ValueError("Queued MULTI Operation has no pending Step.")
            source_references = {
                step.sequence: self._step_executor.source_reference(step)
                for step in pending_steps
            }
            await self._notebook_projector.prepare(
                driver,
                lease,
                execution.id,
                execution.runtime_profile,
                workspace,
                pending_steps,
                target.id,
                source_references,
            )
            for pending in pending_steps:
                source_reference = source_references[pending.sequence]
                code = await self._result_store.read_source(source_reference)
                artifact_snapshot = await self._artifacts.snapshot(
                    driver, workspace
                )
                await self._step_executor.mark_started(lease, pending.sequence)
                try:
                    result = await self._trace_runtime(
                        "executor.runtime.code.execute",
                        self._step_executor.execute(
                            driver,
                            runtime_session_id,
                            code,
                            execution.id,
                            pending.sequence,
                            result_identity=self._step_executor.result_identity(
                                pending, lease
                            ),
                            source_reference=source_reference,
                        ),
                        execution_id=execution.id,
                        target_id=target.id,
                        sequence=pending.sequence,
                    )
                except asyncio.CancelledError:
                    try:
                        await self._artifacts.discover_and_register(
                            driver=driver,
                            workspace=workspace,
                            before=artifact_snapshot,
                            lease=lease,
                            sequence=pending.sequence,
                            status=ArtifactStatus.INCOMPLETE,
                            allow_cancel_requested=True,
                        )
                    except Exception as artifact_exc:
                        await self._record_artifact_failure(
                            lease,
                            pending.sequence,
                            artifact_exc,
                        )
                    raise
                except (
                    StoredRuntimeExecutionTimeoutError,
                    StoredRuntimeOutputLimitExceededError,
                ) as exc:
                    failure_type = (
                        FailureType.OUTPUT_LIMIT_EXCEEDED
                        if isinstance(
                            exc, StoredRuntimeOutputLimitExceededError
                        )
                        else (
                            FailureType.STEP_TIMEOUT
                            if exc.scope == "Step"
                            else FailureType.OPERATION_TIMEOUT
                        )
                    )
                    resolution = await self._resolve_runtime_abort(
                        lease,
                        driver,
                        runtime_session_id,
                        failure_type,
                    )
                    await self._step_executor.mark_failed(
                        lease,
                        pending.sequence,
                        exc.stored_result,
                        str(exc),
                    )
                    await self._skip_operation_steps_after(
                        lease, operation_id, pending.sequence
                    )
                    await self._notebook_projector.project(
                        driver,
                        lease,
                        execution.runtime_profile,
                        workspace,
                    )
                    try:
                        await self._artifacts.discover_and_register(
                            driver=driver,
                            workspace=workspace,
                            before=artifact_snapshot,
                            lease=lease,
                            sequence=pending.sequence,
                            status=ArtifactStatus.INCOMPLETE,
                        )
                    except Exception as artifact_exc:
                        await self._record_artifact_failure(
                            lease,
                            pending.sequence,
                            artifact_exc,
                        )
                    if resolution.retain_session:
                        await self._complete_multi_operation(
                            lease,
                            operation_id,
                            OperationStatus.FAILED,
                            error_message=str(exc),
                        )
                    else:
                        await self.finalize(
                            lease,
                            ExecutionStatus.FAILED,
                            str(exc),
                            failure_type=failure_type,
                            retry_strategy=RetryStrategy.NOT_RETRYABLE,
                            runtime_session_cleanup_status=(
                                resolution.cleanup_status
                            ),
                        )
                    return
                except StoredRuntimeExecutionError as exc:
                    await self._step_executor.mark_failed(
                        lease,
                        pending.sequence,
                        exc.stored_result,
                        str(exc),
                    )
                    await self._skip_operation_steps_after(
                        lease, operation_id, pending.sequence
                    )
                    await self._notebook_projector.project(
                        driver,
                        lease,
                        execution.runtime_profile,
                        workspace,
                    )
                    try:
                        await self._artifacts.discover_and_register(
                            driver=driver,
                            workspace=workspace,
                            before=artifact_snapshot,
                            lease=lease,
                            sequence=pending.sequence,
                            status=ArtifactStatus.INCOMPLETE,
                        )
                    except Exception as artifact_exc:
                        await self._record_artifact_failure(
                            lease,
                            pending.sequence,
                            artifact_exc,
                        )
                    await self._complete_multi_operation(
                        lease,
                        operation_id,
                        OperationStatus.FAILED,
                        error_message=str(exc),
                    )
                    return
                await self._step_executor.mark_succeeded(
                    lease,
                    pending.sequence,
                    result,
                )
                await self._notebook_projector.project(
                    driver,
                    lease,
                    execution.runtime_profile,
                    workspace,
                )
                try:
                    await self._artifacts.discover_and_register(
                        driver=driver,
                        workspace=workspace,
                        before=artifact_snapshot,
                        lease=lease,
                        sequence=pending.sequence,
                        status=ArtifactStatus.AVAILABLE,
                    )
                except Exception as artifact_exc:
                    await self._record_artifact_failure(
                        lease,
                        pending.sequence,
                        artifact_exc,
                    )
                    raise
            await self._complete_multi_operation(
                lease,
                operation_id,
                OperationStatus.SUCCEEDED,
            )
        except asyncio.CancelledError:
            if await self._cancellation_job_owns_terminal(execution.id):
                # Avoid racing the replacement cancellation job for session deletion and the
                # terminal event. The interrupted-cell handler above already preserved evidence.
                await self.release_for_cancellation(lease)
                raise
            cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
            if runtime_session_id is not None:
                cleanup_status = await best_effort_session_stop(
                    driver, runtime_session_id
                )
            await self.finalize(
                lease,
                ExecutionStatus.FAILED,
                "Executor worker stopped while a MULTI Operation Step was running.",
                failure_type=FailureType.WORKER_SHUTDOWN,
                retry_strategy=RetryStrategy.NOT_RETRYABLE,
                runtime_session_cleanup_status=cleanup_status,
            )
            raise
        except ExecutionLeaseLostError:
            logger.warning(
                "Execution Worker lost its lease fence; stale results were "
                "discarded",
                extra={
                    "execution_id": str(lease.execution_id),
                    "execution_attempt_id": str(lease.attempt_id),
                    "fencing_token": lease.fencing_token,
                },
            )
        except Exception as exc:
            cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
            if runtime_session_id is not None:
                cleanup_status = await best_effort_session_stop(
                    driver, runtime_session_id
                )
            await self.finalize(
                lease,
                ExecutionStatus.FAILED,
                _safe_error(exc),
                failure_type=_failure_policy(exc, False)[0],
                retry_strategy=RetryStrategy.NOT_RETRYABLE,
                runtime_session_cleanup_status=cleanup_status,
            )
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            await driver.close()

    async def _cancellation_job_owns_terminal(
        self, execution_id: UUID
    ) -> bool:
        async with self._session_factory() as session:
            status = await session.scalar(
                select(ExecutionORM.status).where(
                    ExecutionORM.id == execution_id
                )
            )
        return status in {
            ExecutionStatus.CANCEL_REQUESTED,
            ExecutionStatus.CANCELLED,
        }

    async def _complete_multi_operation(
        self,
        lease: ExecutionLease,
        operation_id: UUID,
        operation_status: OperationStatus,
        *,
        error_message: str | None = None,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution, attempt = await require_active_lease(session, lease)
            operation = await session.scalar(
                select(ExecutionOperationORM)
                .where(
                    ExecutionOperationORM.id == operation_id,
                    ExecutionOperationORM.execution_id == lease.execution_id,
                )
                .with_for_update()
            )
            if (
                operation is None
                or operation.status != OperationStatus.RUNNING
            ):
                return
            execution.status = ExecutionStatus.WAITING_FOR_OPERATION
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.updated_at = now
            execution.finalization_requested = False
            if execution.operation_wait_timeout_seconds is None:
                raise ValueError(
                    "MULTI execution has no Operation wait timeout."
                )
            wait_deadline = now + timedelta(
                seconds=execution.operation_wait_timeout_seconds
            )
            execution.operation_wait_expires_at = min(
                wait_deadline,
                (
                    _as_utc(execution.execution_expires_at)
                    if execution.execution_expires_at is not None
                    else wait_deadline
                ),
            )
            execution.version += 1
            operation.status = operation_status
            operation.error_message = (
                error_message[:2000] if error_message else None
            )
            operation.finished_at = now
            operation.updated_at = now
            attempt.status = AttemptStatus.WAITING
            attempt.lease_owner = None
            attempt.lease_expires_at = None
            await add_operation_completed_event(
                session, lease.execution_id, operation_id
            )

    async def _skip_operation_steps_after(
        self,
        lease: ExecutionLease,
        operation_id: UUID,
        failed_sequence: int,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            await require_active_lease(session, lease)
            await session.execute(
                update(ExecutionStepORM)
                .where(
                    ExecutionStepORM.execution_id == lease.execution_id,
                    ExecutionStepORM.operation_id == operation_id,
                    ExecutionStepORM.sequence > failed_sequence,
                    ExecutionStepORM.status == StepStatus.PENDING,
                )
                .values(
                    status=StepStatus.SKIPPED, finished_at=now, updated_at=now
                )
            )

    async def _resolve_runtime_abort(
        self,
        lease: ExecutionLease,
        driver: RuntimeDriver,
        runtime_session_id: str,
        failure_type: FailureType,
    ) -> RuntimeAbortResolution:
        await self._record_runtime_abort(
            lease,
            RuntimeAbortStatus.PENDING,
            RuntimeSessionCleanupStatus.PENDING,
            failure_type,
        )
        try:
            result = await driver.abort_session(
                runtime_session_id,
                self._settings.runtime_abort_timeout_seconds,
            )
        except Exception as exc:
            result = RuntimeAbortResult(
                RuntimeAbortStatus.FAILED,
                f"{type(exc).__name__}: Runtime abort request failed.",
            )
        if result.status == RuntimeAbortStatus.IDLE_CONFIRMED:
            resolution = RuntimeAbortResolution(
                abort_status=result.status,
                cleanup_status=RuntimeSessionCleanupStatus.NOT_REQUIRED,
                retry_strategy=RetryStrategy.FROM_FAILED_STEP,
                retain_session=True,
            )
        elif result.status == RuntimeAbortStatus.SESSION_MISSING:
            resolution = RuntimeAbortResolution(
                abort_status=result.status,
                cleanup_status=RuntimeSessionCleanupStatus.SUCCEEDED,
                retry_strategy=RetryStrategy.FROM_START,
                retain_session=False,
            )
        else:
            try:
                await driver.delete_session(runtime_session_id)
            except Exception as exc:
                result = RuntimeAbortResult(
                    RuntimeAbortStatus.FAILED,
                    f"{type(exc).__name__}: Runtime session deletion failed.",
                )
                resolution = RuntimeAbortResolution(
                    abort_status=RuntimeAbortStatus.FAILED,
                    cleanup_status=RuntimeSessionCleanupStatus.FAILED,
                    retry_strategy=RetryStrategy.FROM_START,
                    retain_session=False,
                )
            else:
                resolution = RuntimeAbortResolution(
                    abort_status=RuntimeAbortStatus.SESSION_DELETED,
                    cleanup_status=RuntimeSessionCleanupStatus.SUCCEEDED,
                    retry_strategy=RetryStrategy.FROM_START,
                    retain_session=False,
                )
        await self._record_runtime_abort(
            lease,
            resolution.abort_status,
            resolution.cleanup_status,
            failure_type,
        )
        return resolution

    async def _record_runtime_abort(
        self,
        lease: ExecutionLease,
        abort_status: RuntimeAbortStatus,
        cleanup_status: RuntimeSessionCleanupStatus,
        failure_type: FailureType,
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution, attempt = await require_active_lease(session, lease)
            execution.runtime_abort_status = abort_status
            execution.runtime_session_cleanup_status = cleanup_status
            execution.failure_type = failure_type
            execution.updated_at = now
            execution.version += 1
            attempt.runtime_abort_status = abort_status
            attempt.runtime_session_cleanup_status = cleanup_status
            attempt.failure_type = failure_type

    async def release_for_cancellation(self, lease: ExecutionLease) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution, attempt = await require_active_lease(
                session,
                lease,
                allowed_statuses=(ExecutionStatus.CANCEL_REQUESTED,),
            )
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.heartbeat_at = None
            execution.updated_at = now
            attempt.lease_owner = None
            attempt.lease_expires_at = None

    async def _record_artifact_failure(
        self,
        lease: ExecutionLease,
        sequence: int,
        error: Exception,
    ) -> None:
        logger.warning(
            "Artifact registration failed",
            extra={
                "execution_id": str(lease.execution_id),
                "sequence": sequence,
                "error_type": type(error).__name__,
            },
        )

    async def finalize(
        self,
        lease: ExecutionLease,
        requested_status: ExecutionStatus,
        error_message: str | None = None,
        *,
        retain_session: bool = False,
        retry_from_sequence: int | None = None,
        failure_type: FailureType | None = None,
        retry_strategy: RetryStrategy = RetryStrategy.NOT_RETRYABLE,
        runtime_session_cleanup_status: RuntimeSessionCleanupStatus = (
            RuntimeSessionCleanupStatus.NOT_REQUIRED
        ),
    ) -> None:
        now = utc_now()
        async with self._session_factory() as session, session.begin():
            execution, attempt = await require_active_lease(session, lease)
            running_step_ids = list(
                await session.scalars(
                    select(ExecutionStepAttemptORM.execution_step_id).where(
                        ExecutionStepAttemptORM.execution_attempt_id
                        == lease.attempt_id,
                        ExecutionStepAttemptORM.status == StepStatus.RUNNING,
                    )
                )
            )
            abort_was_pending = (
                execution.runtime_abort_status == RuntimeAbortStatus.PENDING
            )
            if (
                abort_was_pending
                and runtime_session_cleanup_status
                == RuntimeSessionCleanupStatus.NOT_REQUIRED
            ):
                runtime_session_cleanup_status = (
                    RuntimeSessionCleanupStatus.FAILED
                )
            status = requested_status
            attempt_status = AttemptStatus(status.value)
            is_failed = status == ExecutionStatus.FAILED
            effective_failure_type = failure_type if is_failed else None
            effective_retry_strategy = (
                retry_strategy if is_failed else RetryStrategy.NOT_RETRYABLE
            )
            execution.status = status
            execution.error_message = error_message if is_failed else None
            execution.failure_type = effective_failure_type
            execution.finished_at = now
            execution.updated_at = now
            execution.lease_owner = None
            execution.lease_expires_at = None
            execution.operation_wait_expires_at = None
            execution.retry_strategy = effective_retry_strategy
            execution.retry_from_sequence = None
            if effective_retry_strategy == RetryStrategy.FROM_FAILED_STEP:
                execution.retry_from_sequence = retry_from_sequence
            elif effective_retry_strategy == RetryStrategy.FROM_START:
                execution.retry_from_sequence = 0
            execution.retained_runtime_session_until = (
                now
                + timedelta(
                    seconds=self._settings.failed_session_retention_seconds
                )
                if is_failed and retain_session
                else None
            )
            execution.runtime_session_cleanup_status = (
                runtime_session_cleanup_status
            )
            if abort_was_pending:
                execution.runtime_abort_status = (
                    RuntimeAbortStatus.SESSION_DELETED
                    if runtime_session_cleanup_status
                    == RuntimeSessionCleanupStatus.SUCCEEDED
                    else RuntimeAbortStatus.FAILED
                )
            if (
                not retain_session
                and runtime_session_cleanup_status
                == RuntimeSessionCleanupStatus.SUCCEEDED
            ):
                execution.runtime_session_id = None
            execution.version += 1
            attempt.status = attempt_status
            attempt.lease_owner = None
            attempt.lease_expires_at = None
            attempt.error_message = error_message if is_failed else None
            attempt.failure_type = effective_failure_type
            attempt.retry_strategy = effective_retry_strategy
            attempt.runtime_session_cleanup_status = (
                runtime_session_cleanup_status
            )
            if abort_was_pending:
                attempt.runtime_abort_status = execution.runtime_abort_status
            attempt.finished_at = now
            completed_operation_id: UUID | None = None
            if execution.active_operation_id is not None:
                operation_update = await session.execute(
                    update(ExecutionOperationORM)
                    .where(
                        ExecutionOperationORM.id
                        == execution.active_operation_id,
                        ExecutionOperationORM.status.in_(
                            [OperationStatus.QUEUED, OperationStatus.RUNNING]
                        ),
                    )
                    .values(
                        status=(
                            OperationStatus.SUCCEEDED
                            if status == ExecutionStatus.SUCCEEDED
                            else OperationStatus.FAILED
                        ),
                        execution_attempt_id=lease.attempt_id,
                        error_message=error_message if is_failed else None,
                        finished_at=now,
                        updated_at=now,
                    )
                )
                operation = await session.scalar(
                    select(ExecutionOperationORM)
                    .where(
                        ExecutionOperationORM.id
                        == execution.active_operation_id
                    )
                    .execution_options(populate_existing=True)
                )
                if (
                    operation is not None
                    and getattr(operation_update, "rowcount", None) == 1
                ):
                    completed_operation_id = operation.id
            if status == ExecutionStatus.FAILED:
                await session.execute(
                    update(ExecutionStepORM)
                    .where(
                        ExecutionStepORM.execution_id == lease.execution_id,
                        ExecutionStepORM.status == StepStatus.RUNNING,
                    )
                    .values(
                        status=StepStatus.FAILED,
                        error_message=error_message,
                        finished_at=now,
                        updated_at=now,
                    )
                )
                for step_id in running_step_ids:
                    await add_step_history_completed_event(
                        session,
                        lease.execution_id,
                        step_id,
                        lease.attempt_id,
                        StepStatus.FAILED,
                        error_message=(
                            error_message or "Step execution failed."
                        ),
                        retryable=(
                            effective_retry_strategy
                            != RetryStrategy.NOT_RETRYABLE
                        ),
                    )
                await session.execute(
                    update(ExecutionStepORM)
                    .where(
                        ExecutionStepORM.execution_id == lease.execution_id,
                        ExecutionStepORM.status == StepStatus.PENDING,
                    )
                    .values(
                        status=StepStatus.SKIPPED,
                        finished_at=now,
                        updated_at=now,
                    )
                )
                await session.execute(
                    update(ExecutionStepAttemptORM)
                    .where(
                        ExecutionStepAttemptORM.execution_attempt_id
                        == lease.attempt_id,
                        ExecutionStepAttemptORM.status == StepStatus.RUNNING,
                    )
                    .values(
                        status=StepStatus.FAILED,
                        error_message=error_message,
                        finished_at=now,
                    )
                )
            if completed_operation_id is not None:
                await add_operation_completed_event(
                    session, lease.execution_id, completed_operation_id
                )
            await add_execution_completed_event(session, lease.execution_id)


def _as_utc(value: datetime) -> datetime:
    """SQLite tests may return timezone-naive values for aware columns."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
