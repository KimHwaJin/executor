"""Claims Execution work and routes it to a mode-specific Runner."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.domain.enums import ExecutionStatus, OperationMode
from executor_service.domain.results import ExecutionResultStore
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import (
    ExecutionHandoverORM,
    ExecutionORM,
)
from executor_service.infrastructure.execution_worker.claiming import (
    ExecutionClaimer,
)
from executor_service.infrastructure.execution_worker.handover import (
    StepHandover,
)
from executor_service.infrastructure.execution_worker.lease_heartbeat import (
    LeaseHeartbeatManager,
)
from executor_service.infrastructure.execution_worker.multi_operation_state import (
    MultiOperationState,
)
from executor_service.infrastructure.execution_worker.multi_runner import (
    MultiExecutionRunner,
)
from executor_service.infrastructure.execution_worker.notebook_projector import (
    NotebookProjector,
)
from executor_service.infrastructure.execution_worker.run_finalizer import (
    ExecutionRunFinalizer,
)
from executor_service.infrastructure.execution_worker.runtime_calls import (
    RuntimeDriverProvider,
)
from executor_service.infrastructure.execution_worker.single_runner import (
    SingleExecutionRunner,
)
from executor_service.infrastructure.execution_worker.step_executor import (
    ExecutionStepExecutor,
)
from executor_service.infrastructure.execution_worker.step_flow import (
    StepFlow,
)
from executor_service.infrastructure.workspace import WorkspaceManager
from executor_service.settings import Settings


class ExecutionRunner:
    """Claims work and delegates it to the SINGLE or MULTI runner."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        driver_provider: RuntimeDriverProvider,
        artifacts: ExecutionArtifactManager,
        result_store: ExecutionResultStore,
        workspace: WorkspaceManager,
        claimer: ExecutionClaimer,
        lease_heartbeat: LeaseHeartbeatManager,
        notebook_projector: NotebookProjector,
        step_executor: ExecutionStepExecutor,
    ) -> None:
        self._session_factory = session_factory
        self._claimer = claimer
        self._finalizer = ExecutionRunFinalizer(session_factory, settings)
        operation_state = MultiOperationState(session_factory)
        steps = StepFlow(
            settings,
            artifacts,
            lease_heartbeat,
            notebook_projector,
            step_executor,
            self._finalizer,
        )
        self._single = SingleExecutionRunner(
            result_store,
            workspace,
            claimer,
            lease_heartbeat,
            notebook_projector,
            step_executor,
            self._finalizer,
            driver_provider,
            steps,
        )
        self._multi = MultiExecutionRunner(
            result_store,
            workspace,
            lease_heartbeat,
            notebook_projector,
            step_executor,
            self._finalizer,
            operation_state,
            driver_provider,
            steps,
        )

    async def run(self, execution_id: UUID) -> None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        ExecutionORM.status,
                        ExecutionHandoverORM.execution_id.label("handover"),
                    )
                    .outerjoin(
                        ExecutionHandoverORM,
                        ExecutionHandoverORM.execution_id == ExecutionORM.id,
                    )
                    .where(ExecutionORM.id == execution_id)
                )
            ).first()
        if row is None:
            return
        handover: StepHandover | None = None
        if row.status == ExecutionStatus.RUNNING and row.handover:
            taken_over = await self._claimer.claim_handover(execution_id)
            if taken_over is None:
                return
            execution, target, lease, handover = taken_over
        else:
            claimed = await self._claimer.claim(execution_id)
            if claimed is None:
                return
            execution, target, lease = claimed
        if execution.operation_mode == OperationMode.MULTI:
            await self._multi.run(execution, target, lease, handover)
        else:
            await self._single.run(execution, target, lease, handover)
