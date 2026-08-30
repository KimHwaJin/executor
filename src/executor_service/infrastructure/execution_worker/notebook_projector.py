"""Projection of durable execution results into Runtime notebooks."""

import asyncio
import logging
from collections.abc import Awaitable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.models import (
    ExecutionStep,
    NotebookProjectionStatus,
    utc_now,
)
from executor_service.domain.results import (
    ExecutionResultStore,
    ExecutionSourceReference,
    StepResultReference,
)
from executor_service.domain.runtime import (
    RuntimeDriver,
    RuntimeDriverError,
    RuntimeNotebookPreparer,
    RuntimeNotebookSourceCell,
)
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionORM,
    ExecutionStepORM,
)
from executor_service.infrastructure.execution_leases import (
    ExecutionLease,
    ExecutionLeaseLostError,
    require_active_lease,
)
from executor_service.infrastructure.execution_worker.failure_policy import (
    safe_error,
)
from executor_service.infrastructure.runtime_diagnostics import (
    log_runtime_failure,
)
from executor_service.infrastructure.workspace import (
    ExecutionWorkspace,
    WorkspaceManager,
)
from executor_service.tracing import TracingManager

logger = logging.getLogger(__name__)


class NotebookProjector:
    """Writes the latest durable Step results into the Runtime notebook."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        result_store: ExecutionResultStore,
        workspace: WorkspaceManager,
        artifacts: ExecutionArtifactManager,
        tracing: TracingManager,
    ) -> None:
        self._session_factory = session_factory
        self._result_store = result_store
        self._workspace = workspace
        self._artifacts = artifacts
        self._tracing = tracing

    async def prepare(
        self,
        driver: RuntimeDriver,
        lease: ExecutionLease,
        execution_id: UUID,
        runtime_profile: str,
        workspace: ExecutionWorkspace,
        steps: list[ExecutionStep],
        runtime_target_id: UUID,
        source_references: dict[int, ExecutionSourceReference],
    ) -> None:
        if not isinstance(driver, RuntimeNotebookPreparer):
            return
        source_cells: list[RuntimeNotebookSourceCell] = []
        for step in sorted(steps, key=lambda value: value.sequence):
            if step.operation_id is None:
                raise ValueError("Execution Step has no owning Operation.")
            source_cells.append(
                RuntimeNotebookSourceCell(
                    sequence=step.sequence,
                    operation_id=step.operation_id,
                    step_id=step.id,
                    source=await self._result_store.read_source(
                        source_references[step.sequence]
                    ),
                )
            )
        cells = tuple(source_cells)
        if not cells:
            return
        await self._assert_active_lease(lease)
        result = await self._trace_runtime(
            "executor.runtime.notebook.prepare",
            driver.prepare_notebook(
                workspace.runtime_relative_path,
                execution_id,
                runtime_profile,
                cells,
            ),
            execution_id=execution_id,
            target_id=runtime_target_id,
        )
        if result.notebook_path != workspace.notebook_path:
            raise RuntimeDriverError(
                "Runtime prepared an unexpected notebook path."
            )

    async def project(
        self,
        driver: RuntimeDriver,
        lease: ExecutionLease,
        runtime_profile: str,
        workspace: ExecutionWorkspace,
    ) -> bool:
        await self._assert_active_lease(lease)
        async with self._session_factory() as session, session.begin():
            execution, _attempt = await require_active_lease(session, lease)
            execution.notebook_projection_status = "PENDING"
            execution.notebook_projection_error = None
            execution.updated_at = utc_now()
            steps = list(
                await session.scalars(
                    select(ExecutionStepORM)
                    .where(ExecutionStepORM.execution_id == lease.execution_id)
                    .order_by(ExecutionStepORM.sequence)
                )
            )
        try:
            cells: list[str] = []
            outputs: list[list[dict[str, object]]] = []
            execution_counts: list[int | None] = []
            for step in steps:
                cells.append(
                    await self._result_store.read_source(
                        ExecutionSourceReference(
                            relative_path=step.source_snapshot_path,
                            checksum_sha256=step.source_sha256,
                            size_bytes=step.source_size_bytes,
                        )
                    )
                )
                if (
                    step.result_manifest_path is not None
                    and step.result_manifest_checksum_sha256 is not None
                    and step.result_execution_attempt_id is not None
                    and step.result_fencing_token is not None
                ):
                    projection = await self._result_store.read_step_projection(
                        StepResultReference(
                            relative_path=step.result_manifest_path,
                            checksum_sha256=step.result_manifest_checksum_sha256,
                            size_bytes=step.result_manifest_size_bytes or 0,
                            execution_attempt_id=step.result_execution_attempt_id,
                            fencing_token=step.result_fencing_token,
                        )
                    )
                    outputs.append(projection.outputs)
                    execution_counts.append(projection.execution_count)
                else:
                    outputs.append([])
                    execution_counts.append(None)
            notebook = self._workspace.notebook_document(
                workspace,
                runtime_profile,
                cells,
                outputs,
                execution_counts,
            )
        except Exception as exc:
            log_runtime_failure(
                logger,
                exc,
                phase="NOTEBOOK_BUILD",
                execution_id=lease.execution_id,
                attempt_id=lease.attempt_id,
            )
            await self._record_projection(
                lease,
                status="FAILED",
                attempt_count=0,
                error_message=safe_error(exc),
            )
            raise
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                await self._assert_active_lease(lease)
                await driver.write_notebook(workspace.notebook_path, notebook)
            except ExecutionLeaseLostError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    await asyncio.sleep(0.1 * (2 ** (attempt - 1)))
                    continue
            else:
                await self._record_projection(
                    lease,
                    status="SUCCEEDED",
                    attempt_count=attempt,
                )
                return True
            break
        await self._record_projection(
            lease,
            status="FAILED",
            attempt_count=3,
            error_message=(
                safe_error(last_error)
                if last_error is not None
                else "Notebook projection failed."
            ),
        )
        if last_error is not None:
            log_runtime_failure(
                logger,
                last_error,
                phase="NOTEBOOK_WRITE",
                execution_id=lease.execution_id,
                attempt_id=lease.attempt_id,
            )
        return False

    async def project_after_failure(
        self,
        driver: RuntimeDriver,
        lease: ExecutionLease,
        runtime_profile: str,
        workspace: ExecutionWorkspace,
    ) -> bool:
        """Never replace the initiating Step failure with projection failure."""
        try:
            return await self.project(
                driver, lease, runtime_profile, workspace
            )
        except ExecutionLeaseLostError:
            raise
        except Exception as exc:
            log_runtime_failure(
                logger,
                exc,
                phase="NOTEBOOK_AFTER_FAILURE",
                execution_id=lease.execution_id,
                attempt_id=lease.attempt_id,
            )
            return False

    async def register_artifact(
        self,
        *,
        driver: RuntimeDriver,
        workspace: ExecutionWorkspace,
        lease: ExecutionLease,
        sequence: int,
    ) -> None:
        async with self._session_factory() as session:
            projection_status = await session.scalar(
                select(ExecutionORM.notebook_projection_status).where(
                    ExecutionORM.id == lease.execution_id
                )
            )
        if projection_status != "SUCCEEDED":
            return
        try:
            await self._artifacts.register_notebook(
                driver=driver,
                workspace=workspace,
                lease=lease,
                sequence=sequence,
            )
        except ExecutionLeaseLostError:
            raise
        except Exception as exc:
            log_runtime_failure(
                logger,
                exc,
                phase="NOTEBOOK_ARTIFACT_REGISTER",
                execution_id=lease.execution_id,
                attempt_id=lease.attempt_id,
                sequence=sequence,
            )

    async def _record_projection(
        self,
        lease: ExecutionLease,
        *,
        status: NotebookProjectionStatus,
        attempt_count: int,
        error_message: str | None = None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            execution, _attempt = await require_active_lease(session, lease)
            execution.notebook_projection_status = status
            execution.notebook_projection_attempt_count += attempt_count
            execution.notebook_projection_error = error_message
            execution.notebook_projected_at = (
                utc_now() if status == "SUCCEEDED" else None
            )
            execution.updated_at = utc_now()

    async def _assert_active_lease(self, lease: ExecutionLease) -> None:
        async with self._session_factory() as session:
            await require_active_lease(session, lease)

    async def _trace_runtime[T](
        self,
        name: str,
        operation: Awaitable[T],
        *,
        execution_id: UUID,
        target_id: UUID,
    ) -> T:
        with self._tracing.span(
            name,
            attributes={
                "executor.execution.id": str(execution_id),
                "executor.runtime.target.id": str(target_id),
            },
        ):
            return await operation
