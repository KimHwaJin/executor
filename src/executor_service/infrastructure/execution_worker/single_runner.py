"""SINGLE Execution Runtime runner."""

import asyncio
import logging

from executor_service.config import Settings
from executor_service.domain.enums import (
    ArtifactStatus,
    ExecutionStatus,
    FailureType,
    RetryStrategy,
    RuntimeSessionCleanupStatus,
)
from executor_service.domain.models import Execution
from executor_service.domain.results import ExecutionResultStore
from executor_service.domain.runtime import (
    ExecutionCompletionError,
    RuntimeDriverError,
    RuntimeExecutionError,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import RuntimeTargetORM
from executor_service.infrastructure.execution_leases import (
    ExecutionLease,
    ExecutionLeaseLostError,
)
from executor_service.infrastructure.execution_worker.claiming import (
    ExecutionClaimer,
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
        settings: Settings,
        artifacts: ExecutionArtifactManager,
        result_store: ExecutionResultStore,
        workspace: WorkspaceManager,
        claimer: ExecutionClaimer,
        lease_heartbeat: LeaseHeartbeatManager,
        notebook_projector: NotebookProjector,
        step_executor: ExecutionStepExecutor,
        finalizer: ExecutionRunFinalizer,
        driver_provider: RuntimeDriverProvider,
    ) -> None:
        self._settings = settings
        self._artifacts = artifacts
        self._result_store = result_store
        self._workspace = workspace
        self._claimer = claimer
        self._lease_heartbeat = lease_heartbeat
        self._notebook_projector = notebook_projector
        self._step_executor = step_executor
        self._finalizer = finalizer
        self._driver_provider = driver_provider

    async def run(
        self,
        execution: Execution,
        target: RuntimeTargetORM,
        lease: ExecutionLease,
    ) -> None:
        driver = self._driver_provider.create(target)
        runtime_session_id: str | None = None
        heartbeat = asyncio.create_task(
            self._lease_heartbeat.run_execution(lease),
            name=f"heartbeat-{execution.id}",
        )
        failed_sequence: int | None = None
        try:
            resume = (
                execution.retry_count > 0
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
            await run_runtime_operation(
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
                raise RuntimeError("Runtime session ID was not established.")
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
                    result = await run_runtime_operation(
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
                            lease=lease,
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
                        await self._finalizer.record_artifact_failure(
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
                            sequence=sequence,
                            status=ArtifactStatus.INCOMPLETE,
                        )
                    except Exception as artifact_exc:
                        await self._finalizer.record_artifact_failure(
                            lease,
                            sequence,
                            artifact_exc,
                        )
                        logger.warning(
                            "Incomplete Artifact registration failed",
                            extra={"execution_id": str(execution.id)},
                        )
                    raise
                except StoredStepFailure as exc:
                    failed_sequence = sequence
                    if exc.stored_result is not None:
                        await self._step_executor.mark_failed(
                            lease,
                            sequence,
                            exc.stored_result,
                            _safe_error(exc),
                            retryable=(
                                _failure_policy(exc, False)[1]
                                != RetryStrategy.NOT_RETRYABLE
                            ),
                        )
                    raise
                await self._step_executor.mark_succeeded(
                    lease,
                    sequence,
                    result,
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
                        sequence=sequence,
                        status=ArtifactStatus.AVAILABLE,
                    )
                except ExecutionLeaseLostError:
                    raise
                except Exception as artifact_exc:
                    await self._finalizer.record_artifact_failure(
                        lease,
                        sequence,
                        artifact_exc,
                    )
                    raise ExecutionCompletionError(
                        "ARTIFACT_REGISTER"
                    ) from artifact_exc
            await self._notebook_projector.register_artifact(
                driver=driver,
                workspace=workspace,
                lease=lease,
                sequence=len(cells) - 1,
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
        except asyncio.CancelledError:
            if await self._finalizer.cancellation_owns_terminal(execution.id):
                # The replacement cancellation job exclusively owns Runtime cleanup and the
                # CANCELLED transition. This execution job only preserves cell evidence.
                await self._finalizer.release_for_cancellation(lease)
                raise
            cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
            if runtime_session_id is not None:
                try:
                    async with asyncio.timeout(
                        self._settings.execution_shutdown_cleanup_seconds
                    ):
                        cleanup_status = await best_effort_session_stop(
                            driver,
                            runtime_session_id,
                            lease=lease,
                            diagnostics=self._finalizer.diagnostics,
                        )
                except TimeoutError:
                    cleanup_status = RuntimeSessionCleanupStatus.FAILED
                    logger.warning(
                        "Runtime session cleanup exceeded the Worker shutdown deadline",
                        extra={"execution_id": str(execution.id)},
                    )
            await self._finalizer.finalize(
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
            resolution = await self._finalizer.resolve_runtime_abort(
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
                    sequence=failed_sequence,
                    status=ArtifactStatus.INCOMPLETE,
                )
            except Exception as artifact_exc:
                await self._finalizer.record_artifact_failure(
                    lease,
                    failed_sequence,
                    artifact_exc,
                )
                logger.warning(
                    "Aborted-step Artifact registration failed",
                    extra={"execution_id": str(execution.id)},
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
            failure_type, retry_strategy = _failure_policy(exc, retain_session)
            cleanup_status = RuntimeSessionCleanupStatus.NOT_REQUIRED
            if runtime_session_id is not None and not retain_session:
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
