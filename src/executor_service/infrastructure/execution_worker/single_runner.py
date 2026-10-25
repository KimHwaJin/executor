"""SINGLE Execution Runtime runner."""

import asyncio
import logging

from executor_service.domain.enums import (
    ExecutionStatus,
    FailureType,
    RetryStrategy,
    RuntimeSessionCleanupStatus,
)
from executor_service.domain.models import Execution
from executor_service.domain.results import ExecutionResultStore
from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeExecutionError,
)
from executor_service.infrastructure.db.models import RuntimeTargetORM
from executor_service.infrastructure.execution_leases import (
    ExecutionLease,
    ExecutionLeaseLostError,
)
from executor_service.infrastructure.execution_worker.claiming import (
    ExecutionClaimer,
)
from executor_service.infrastructure.execution_worker.failure_policy import (
    failure_policy,
    safe_error,
)
from executor_service.infrastructure.execution_worker.handover import (
    StepHandover,
    StepRun,
)
from executor_service.infrastructure.execution_worker.lease_heartbeat import (
    LeaseHeartbeatManager,
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
from executor_service.infrastructure.execution_worker.step_executor import (
    ExecutionStepExecutor,
)
from executor_service.infrastructure.execution_worker.step_flow import (
    StepFlow,
)
from executor_service.infrastructure.execution_worker.types import (
    RetainedRuntimeSessionLostError,
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


class SingleExecutionRunner:
    """Runs a claimed SINGLE Execution from source through finalization."""

    def __init__(
        self,
        result_store: ExecutionResultStore,
        workspace: WorkspaceManager,
        claimer: ExecutionClaimer,
        lease_heartbeat: LeaseHeartbeatManager,
        notebook_projector: NotebookProjector,
        step_executor: ExecutionStepExecutor,
        finalizer: ExecutionRunFinalizer,
        driver_provider: RuntimeDriverProvider,
        steps: StepFlow,
    ) -> None:
        self._result_store = result_store
        self._workspace = workspace
        self._claimer = claimer
        self._lease_heartbeat = lease_heartbeat
        self._notebook_projector = notebook_projector
        self._step_executor = step_executor
        self._finalizer = finalizer
        self._driver_provider = driver_provider
        self._steps = steps

    async def run(
        self,
        execution: Execution,
        target: RuntimeTargetORM,
        lease: ExecutionLease,
        handover: StepHandover | None = None,
    ) -> None:
        driver = self._driver_provider.create(target)
        runtime_session_id: str | None = None
        heartbeat = asyncio.create_task(
            self._lease_heartbeat.run_execution(lease),
            name=f"heartbeat-{execution.id}",
        )
        failed_sequence: int | None = None
        # The Step whose result is not yet committed; another Worker can
        # take it over when this one stops.
        run_in_flight: StepRun | None = None
        try:
            resume = (
                handover is None
                and execution.retry_count > 0
                and execution.retry_strategy == RetryStrategy.FROM_FAILED_STEP
                and execution.retry_from_sequence is not None
                and execution.runtime_session_id is not None
            )
            if resume:
                runtime_session_id = execution.runtime_session_id
                try:
                    session_exists = await run_runtime_operation(
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
            taken_over: StepRun | None = None
            if handover is not None:
                runtime_session_id = execution.runtime_session_id
                taken_over = await self._step_executor.load_run(lease)
                start_sequence = taken_over.sequence
            else:
                await run_runtime_operation(
                    "executor.runtime.workspace.prepare",
                    driver.prepare_workspace(workspace.runtime_relative_path),
                    execution_id=execution.id,
                    target_id=target.id,
                )
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
                    runtime_session_id = await run_runtime_operation(
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
            if runtime_session_id is None or start_sequence is None:
                raise RuntimeError("Runtime session ID was not established.")
            profile = execution.runtime_profile
            for sequence in range(start_sequence, len(cells)):
                if taken_over is not None:
                    step_run, step_handover = taken_over, handover
                    taken_over = None
                else:
                    step_run = await self._steps.start(
                        driver, workspace, lease, sequence
                    )
                    step_handover = None
                run_in_flight = step_run
                try:
                    result = await self._steps.execute(
                        driver,
                        runtime_session_id,
                        cells[sequence],
                        step=steps_by_sequence[sequence],
                        run=step_run,
                        lease=lease,
                        source_reference=source_references[sequence],
                        handover=step_handover,
                        target_id=target.id,
                    )
                except asyncio.CancelledError:
                    if not self._step_executor.shutting_down:
                        # Keep files the interrupted cell wrote before the
                        # cancellation path tears down the session. On
                        # shutdown the next Worker registers them instead.
                        await self._steps.register_incomplete(
                            driver,
                            workspace,
                            lease,
                            sequence,
                            step_run.artifact_baseline,
                            allow_cancel_requested=True,
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
                        lease, sequence, exc.stored_result, str(exc)
                    )
                    run_in_flight = None
                    await self._step_executor.release(
                        driver, runtime_session_id, step_run
                    )
                    await self._steps.record_failure_evidence(
                        driver,
                        workspace,
                        profile,
                        lease,
                        sequence,
                        step_run.artifact_baseline,
                    )
                    raise
                except StoredStepFailure as exc:
                    failed_sequence = sequence
                    if exc.stored_result is not None:
                        await self._step_executor.mark_failed(
                            lease,
                            sequence,
                            exc.stored_result,
                            safe_error(exc),
                            retryable=(
                                failure_policy(exc, False)[1]
                                != RetryStrategy.NOT_RETRYABLE
                            ),
                        )
                    raise
                await self._step_executor.mark_succeeded(
                    lease, sequence, result
                )
                run_in_flight = None
                await self._steps.publish_success(
                    driver,
                    runtime_session_id,
                    workspace,
                    profile,
                    lease,
                    step_run,
                )
            await self._steps.complete_session(
                driver, runtime_session_id, lease, target.id
            )
        except asyncio.CancelledError:
            if await self._steps.hand_off_cancelled(lease, run_in_flight):
                raise
            await self._finalizer.finalize(
                lease,
                ExecutionStatus.FAILED,
                "Executor worker stopped while the execution was running.",
                failure_type=FailureType.WORKER_SHUTDOWN,
                retry_strategy=RetryStrategy.FROM_START,
                runtime_session_cleanup_status=await self._steps.stop_session(
                    driver, runtime_session_id, lease
                ),
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
            failure_type, resolution = await self._steps.abort(
                driver, runtime_session_id, lease, failed_sequence, exc
            )
            await self._steps.record_failure_evidence(
                driver,
                workspace,
                execution.runtime_profile,
                lease,
                failed_sequence,
                step_run.artifact_baseline,
            )
            await self._finalizer.finalize(
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
            self._steps.log_lease_lost(lease)
        except Exception as exc:
            await self._finalizer.record_execution_failure(lease, exc)
            log_runtime_failure(
                logger,
                exc,
                phase="EXECUTION_RUN",
                execution_id=execution.id,
                attempt_id=lease.attempt_id,
                target_id=target.id,
                runtime_session_id=runtime_session_id,
                sequence=failed_sequence,
            )
            retain_session = (
                isinstance(exc, RuntimeExecutionError)
                and runtime_session_id is not None
                and failed_sequence is not None
            )
            failure_type, retry_strategy = failure_policy(exc, retain_session)
            await self._finalizer.finalize(
                lease,
                ExecutionStatus.FAILED,
                safe_error(exc),
                retain_session=retain_session,
                retry_from_sequence=failed_sequence,
                failure_type=failure_type,
                retry_strategy=retry_strategy,
                runtime_session_cleanup_status=(
                    RuntimeSessionCleanupStatus.NOT_REQUIRED
                    if retain_session
                    else await self._steps.stop_session(
                        driver, runtime_session_id, lease
                    )
                ),
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await driver.close()
