"""Step and Runtime session handling shared by the SINGLE and MULTI runners."""

import asyncio
import logging
from uuid import UUID

from executor_service.domain.enums import (
    ArtifactStatus,
    ExecutionStatus,
    FailureType,
    RuntimeSessionCleanupStatus,
)
from executor_service.domain.models import ExecutionStep
from executor_service.domain.results import (
    ExecutionSourceReference,
    StepResultDescriptor,
)
from executor_service.domain.runtime import (
    ExecutionCompletionError,
    RuntimeDriver,
    RuntimeStorageSnapshot,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.execution_leases import (
    ExecutionLease,
    ExecutionLeaseLostError,
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
    run_runtime_operation,
)
from executor_service.infrastructure.execution_worker.runtime_cleanup import (
    best_effort_session_stop,
)
from executor_service.infrastructure.execution_worker.step_executor import (
    ExecutionStepExecutor,
)
from executor_service.infrastructure.execution_worker.types import (
    RuntimeAbortResolution,
    StoredRuntimeExecutionTimeoutError,
    StoredRuntimeOutputLimitExceededError,
)
from executor_service.infrastructure.workspace import ExecutionWorkspace
from executor_service.settings import Settings

logger = logging.getLogger(__name__)


class StepFlow:
    """Runs, records and cleans up Steps on one Runtime session."""

    def __init__(
        self,
        settings: Settings,
        artifacts: ExecutionArtifactManager,
        lease_heartbeat: LeaseHeartbeatManager,
        notebook_projector: NotebookProjector,
        step_executor: ExecutionStepExecutor,
        finalizer: ExecutionRunFinalizer,
    ) -> None:
        self._settings = settings
        self._artifacts = artifacts
        self._lease_heartbeat = lease_heartbeat
        self._notebook_projector = notebook_projector
        self._step_executor = step_executor
        self._finalizer = finalizer

    async def start(
        self,
        driver: RuntimeDriver,
        workspace: ExecutionWorkspace,
        lease: ExecutionLease,
        sequence: int,
    ) -> StepRun:
        return await self._step_executor.mark_started(
            lease, sequence, await self._artifacts.snapshot(driver, workspace)
        )

    async def execute(
        self,
        driver: RuntimeDriver,
        runtime_session_id: str,
        code: str,
        *,
        step: ExecutionStep,
        run: StepRun,
        lease: ExecutionLease,
        source_reference: ExecutionSourceReference,
        handover: StepHandover | None,
        target_id: UUID,
    ) -> StepResultDescriptor:
        return await run_runtime_operation(
            "executor.runtime.code.execute",
            self._step_executor.execute(
                driver,
                runtime_session_id,
                code,
                run=run,
                result_identity=self._step_executor.result_identity(
                    step, lease
                ),
                lease=lease,
                source_reference=source_reference,
                handover=handover,
            ),
            execution_id=lease.execution_id,
            target_id=target_id,
            sequence=step.sequence,
        )

    async def publish_success(
        self,
        driver: RuntimeDriver,
        runtime_session_id: str,
        workspace: ExecutionWorkspace,
        profile: str,
        lease: ExecutionLease,
        run: StepRun,
    ) -> None:
        """Release, project and register a Step whose success is committed."""
        await self._step_executor.release(driver, runtime_session_id, run)
        await self._notebook_projector.project_required(
            driver, lease, profile, workspace
        )
        try:
            await self._artifacts.discover_and_register(
                driver=driver,
                workspace=workspace,
                before=run.artifact_baseline,
                lease=lease,
                sequence=run.sequence,
                status=ArtifactStatus.AVAILABLE,
            )
        except ExecutionLeaseLostError:
            raise
        except Exception as artifact_exc:
            await self._finalizer.record_artifact_failure(
                lease, run.sequence, artifact_exc
            )
            raise ExecutionCompletionError(
                "ARTIFACT_REGISTER"
            ) from artifact_exc

    async def abort(
        self,
        driver: RuntimeDriver,
        runtime_session_id: str,
        lease: ExecutionLease,
        sequence: int,
        exc: StoredRuntimeExecutionTimeoutError
        | StoredRuntimeOutputLimitExceededError,
    ) -> tuple[FailureType, RuntimeAbortResolution]:
        """Interrupt a Step that hit a limit and record its failure."""
        if isinstance(exc, StoredRuntimeOutputLimitExceededError):
            failure_type = FailureType.OUTPUT_LIMIT_EXCEEDED
        elif exc.scope == "Step":
            failure_type = FailureType.STEP_TIMEOUT
        else:
            failure_type = FailureType.OPERATION_TIMEOUT
        resolution = await self._finalizer.resolve_runtime_abort(
            lease, driver, runtime_session_id, failure_type
        )
        await self._step_executor.mark_failed(
            lease, sequence, exc.stored_result, str(exc)
        )
        return failure_type, resolution

    async def record_failure_evidence(
        self,
        driver: RuntimeDriver,
        workspace: ExecutionWorkspace,
        profile: str,
        lease: ExecutionLease,
        sequence: int,
        before: RuntimeStorageSnapshot,
    ) -> None:
        await self._notebook_projector.project_after_failure(
            driver, lease, profile, workspace
        )
        await self.register_incomplete(
            driver, workspace, lease, sequence, before
        )

    async def register_incomplete(
        self,
        driver: RuntimeDriver,
        workspace: ExecutionWorkspace,
        lease: ExecutionLease,
        sequence: int,
        before: RuntimeStorageSnapshot,
        *,
        allow_cancel_requested: bool = False,
    ) -> None:
        """Keep files an interrupted or failed cell wrote, best effort."""
        try:
            await self._artifacts.discover_and_register(
                driver=driver,
                workspace=workspace,
                before=before,
                lease=lease,
                sequence=sequence,
                status=ArtifactStatus.INCOMPLETE,
                allow_cancel_requested=allow_cancel_requested,
            )
        except Exception as artifact_exc:
            await self._finalizer.record_artifact_failure(
                lease, sequence, artifact_exc
            )
            logger.warning(
                "Incomplete Artifact registration failed",
                extra={
                    "execution_id": str(lease.execution_id),
                    "sequence": sequence,
                },
            )

    async def complete_session(
        self,
        driver: RuntimeDriver,
        runtime_session_id: str,
        lease: ExecutionLease,
        target_id: UUID,
    ) -> None:
        await self._lease_heartbeat.assert_execution(lease)
        await run_runtime_operation(
            "executor.runtime.session.delete",
            self._finalizer.release_completed_session(
                lease, driver, runtime_session_id
            ),
            execution_id=lease.execution_id,
            target_id=target_id,
        )
        await self._finalizer.finalize(
            lease,
            ExecutionStatus.SUCCEEDED,
            runtime_session_cleanup_status=RuntimeSessionCleanupStatus.SUCCEEDED,
        )

    async def hand_off_cancelled(
        self, lease: ExecutionLease, run_in_flight: StepRun | None
    ) -> bool:
        """Leave a cancelled job to its new owner; True if one took it."""
        if await self._finalizer.cancellation_owns_terminal(
            lease.execution_id
        ):
            # The cancellation job exclusively owns Runtime cleanup and the
            # CANCELLED transition; this job only preserved cell evidence.
            await self._finalizer.release_for_cancellation(lease)
            return True
        # On shutdown the kernel keeps running and another Worker follows it.
        return (
            self._step_executor.shutting_down
            and run_in_flight is not None
            and await self._finalizer.release_for_handover(lease)
        )

    async def stop_session(
        self,
        driver: RuntimeDriver,
        runtime_session_id: str | None,
        lease: ExecutionLease,
    ) -> RuntimeSessionCleanupStatus:
        """Stop the session if this Worker still owns the Execution."""
        if runtime_session_id is None or not (
            await self._lease_heartbeat.owns_execution(lease)
        ):
            return RuntimeSessionCleanupStatus.NOT_REQUIRED
        try:
            async with asyncio.timeout(
                self._settings.execution_shutdown_cleanup_seconds
            ):
                return await best_effort_session_stop(
                    driver,
                    runtime_session_id,
                    lease=lease,
                    diagnostics=self._finalizer.diagnostics,
                )
        except TimeoutError:
            logger.warning(
                "Runtime session cleanup exceeded the Worker shutdown deadline",
                extra={"execution_id": str(lease.execution_id)},
            )
            return RuntimeSessionCleanupStatus.FAILED

    @staticmethod
    def log_lease_lost(lease: ExecutionLease) -> None:
        logger.warning(
            "Execution Worker lost its lease fence; stale results were "
            "discarded",
            extra={
                "execution_id": str(lease.execution_id),
                "execution_attempt_id": str(lease.attempt_id),
                "fencing_token": lease.fencing_token,
            },
        )
