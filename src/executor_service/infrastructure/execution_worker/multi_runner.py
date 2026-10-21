"""MULTI Execution Runtime runner."""

import asyncio
import logging

from executor_service.domain.enums import (
    ArtifactStatus,
    ExecutionStatus,
    FailureType,
    OperationStatus,
    RetryStrategy,
    RuntimeSessionCleanupStatus,
    StepStatus,
)
from executor_service.domain.models import Execution
from executor_service.domain.results import ExecutionResultStore
from executor_service.domain.runtime import ExecutionCompletionError
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import RuntimeTargetORM
from executor_service.infrastructure.execution_leases import (
    ExecutionLease,
    ExecutionLeaseLostError,
)
from executor_service.infrastructure.execution_worker.failure_policy import (
    failure_policy as _failure_policy,
)
from executor_service.infrastructure.execution_worker.failure_policy import (
    safe_error as _safe_error,
)
from executor_service.infrastructure.execution_worker.handover import (
    StepHandover,
    StepRun,
)
from executor_service.infrastructure.execution_worker.lease_heartbeat import (
    LeaseHeartbeatManager,
)
from executor_service.infrastructure.execution_worker.multi_operation_state import (
    MultiOperationState,
)
from executor_service.infrastructure.execution_worker.notebook_projector import (
    NotebookProjector,
)
from executor_service.infrastructure.execution_worker.run_finalizer import (
    ExecutionRunFinalizer,
)
from executor_service.infrastructure.execution_worker.runtime_calls import (
    RuntimeDriverProvider,
    run_runtime_operation,
)
from executor_service.infrastructure.execution_worker.runtime_cleanup import (
    best_effort_session_stop,
)
from executor_service.infrastructure.execution_worker.step_executor import (
    ExecutionStepExecutor,
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
from executor_service.infrastructure.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class MultiExecutionRunner:
    """Runs one appended MULTI Operation on a retained Runtime session."""

    def __init__(
        self,
        artifacts: ExecutionArtifactManager,
        result_store: ExecutionResultStore,
        workspace: WorkspaceManager,
        lease_heartbeat: LeaseHeartbeatManager,
        notebook_projector: NotebookProjector,
        step_executor: ExecutionStepExecutor,
        finalizer: ExecutionRunFinalizer,
        operation_state: MultiOperationState,
        driver_provider: RuntimeDriverProvider,
    ) -> None:
        self._artifacts = artifacts
        self._result_store = result_store
        self._workspace = workspace
        self._lease_heartbeat = lease_heartbeat
        self._notebook_projector = notebook_projector
        self._step_executor = step_executor
        self._finalizer = finalizer
        self._multi_operation_state = operation_state
        self._driver_provider = driver_provider

    async def run(
        self,
        execution: Execution,
        target: RuntimeTargetORM,
        lease: ExecutionLease,
        handover: StepHandover | None = None,
    ) -> None:
        driver = self._driver_provider.create(target)
        heartbeat = asyncio.create_task(
            self._lease_heartbeat.run_execution(lease),
            name=f"heartbeat-{execution.id}",
        )
        runtime_session_id = execution.runtime_session_id
        # The Step whose result is not yet committed; another Worker can
        # take it over when this one stops.
        run_in_flight: StepRun | None = None
        try:
            workspace = self._workspace.plan(execution)
            taken_over: StepRun | None = None
            if handover is not None:
                taken_over = await self._step_executor.load_run(lease)
            else:
                await run_runtime_operation(
                    "executor.runtime.workspace.prepare",
                    driver.prepare_workspace(workspace.runtime_relative_path),
                    execution_id=execution.id,
                    target_id=target.id,
                )
                if runtime_session_id is None:
                    runtime_session_id = await run_runtime_operation(
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
            if runtime_session_id is None:
                raise ValueError("MULTI Execution has no Runtime session.")
            if taken_over is None and execution.finalization_requested:
                await self._notebook_projector.project_required(
                    driver, lease, execution.runtime_profile, workspace
                )
                await self._lease_heartbeat.assert_execution(lease)
                await run_runtime_operation(
                    "executor.runtime.session.delete",
                    self._finalizer.release_completed_session(
                        lease, driver, runtime_session_id
                    ),
                    execution_id=execution.id,
                    target_id=target.id,
                )
                await self._finalizer.finalize(
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
            pending_steps = sorted(
                (
                    step
                    for step in execution.steps
                    if step.operation_id == operation_id
                    and (
                        step.status == StepStatus.PENDING
                        or (
                            taken_over is not None
                            and step.sequence == taken_over.sequence
                        )
                    )
                ),
                key=lambda step: step.sequence,
            )
            if not pending_steps:
                raise ValueError("Queued MULTI Operation has no pending Step.")
            source_references = {
                step.sequence: self._step_executor.source_reference(step)
                for step in pending_steps
            }
            if taken_over is None:
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
                if taken_over is not None:
                    step_run, step_handover = taken_over, handover
                    taken_over = None
                else:
                    step_run = await self._step_executor.mark_started(
                        lease,
                        pending.sequence,
                        await self._artifacts.snapshot(driver, workspace),
                    )
                    step_handover = None
                artifact_snapshot = step_run.artifact_baseline
                run_in_flight = step_run
                try:
                    result = await run_runtime_operation(
                        "executor.runtime.code.execute",
                        self._step_executor.execute(
                            driver,
                            runtime_session_id,
                            code,
                            run=step_run,
                            result_identity=self._step_executor.result_identity(
                                pending, lease
                            ),
                            lease=lease,
                            source_reference=source_reference,
                            handover=step_handover,
                        ),
                        execution_id=execution.id,
                        target_id=target.id,
                        sequence=pending.sequence,
                    )
                except asyncio.CancelledError:
                    if self._step_executor.shutting_down:
                        # The next Worker registers this Step's Artifacts.
                        raise
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
                        await self._finalizer.record_artifact_failure(
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
                    resolution = await self._finalizer.resolve_runtime_abort(
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
                    await self._multi_operation_state.skip_steps_after(
                        lease, operation_id, pending.sequence
                    )
                    await self._notebook_projector.project_after_failure(
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
                        await self._finalizer.record_artifact_failure(
                            lease,
                            pending.sequence,
                            artifact_exc,
                        )
                    if resolution.retain_session:
                        await self._multi_operation_state.complete(
                            lease,
                            operation_id,
                            OperationStatus.FAILED,
                            error_message=str(exc),
                        )
                    else:
                        await self._finalizer.finalize(
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
                    run_in_flight = None
                    await self._step_executor.release(
                        driver, runtime_session_id, step_run
                    )
                    await self._multi_operation_state.skip_steps_after(
                        lease, operation_id, pending.sequence
                    )
                    await self._notebook_projector.project_after_failure(
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
                        await self._finalizer.record_artifact_failure(
                            lease,
                            pending.sequence,
                            artifact_exc,
                        )
                    await self._multi_operation_state.complete(
                        lease,
                        operation_id,
                        OperationStatus.FAILED,
                        error_message=str(exc),
                    )
                    return
                except StoredStepFailure as exc:
                    if exc.stored_result is not None:
                        await self._step_executor.mark_failed(
                            lease,
                            pending.sequence,
                            exc.stored_result,
                            _safe_error(exc),
                            retryable=False,
                        )
                    raise
                await self._step_executor.mark_succeeded(
                    lease,
                    pending.sequence,
                    result,
                )
                run_in_flight = None
                await self._step_executor.release(
                    driver, runtime_session_id, step_run
                )
                await self._notebook_projector.project_required(
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
                except ExecutionLeaseLostError:
                    raise
                except Exception as artifact_exc:
                    await self._finalizer.record_artifact_failure(
                        lease,
                        pending.sequence,
                        artifact_exc,
                    )
                    raise ExecutionCompletionError(
                        "ARTIFACT_REGISTER"
                    ) from artifact_exc
            await self._multi_operation_state.complete(
                lease,
                operation_id,
                OperationStatus.SUCCEEDED,
            )
        except asyncio.CancelledError:
            if await self._finalizer.cancellation_owns_terminal(execution.id):
                # Avoid racing the replacement cancellation job for session deletion and the
                # terminal event. The interrupted-cell handler above already preserved evidence.
                await self._finalizer.release_for_cancellation(lease)
                raise
            if (
                self._step_executor.shutting_down
                and run_in_flight is not None
                and await self._finalizer.release_for_handover(lease)
            ):
                # The kernel keeps running; another Worker follows it.
                raise
            cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
            if (
                runtime_session_id is not None
                and await self._lease_heartbeat.owns_execution(lease)
            ):
                cleanup_status = await best_effort_session_stop(
                    driver,
                    runtime_session_id,
                    lease=lease,
                    diagnostics=self._finalizer.diagnostics,
                )
            await self._finalizer.finalize(
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
            await self._finalizer.record_execution_failure(lease, exc)
            log_runtime_failure(
                logger,
                exc,
                phase="EXECUTION_RUN",
                execution_id=execution.id,
                attempt_id=lease.attempt_id,
                operation_id=execution.active_operation_id,
                target_id=target.id,
                runtime_session_id=runtime_session_id,
            )
            cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
            if (
                runtime_session_id is not None
                and await self._lease_heartbeat.owns_execution(lease)
            ):
                cleanup_status = await best_effort_session_stop(
                    driver,
                    runtime_session_id,
                    lease=lease,
                    diagnostics=self._finalizer.diagnostics,
                )
            await self._finalizer.finalize(
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
