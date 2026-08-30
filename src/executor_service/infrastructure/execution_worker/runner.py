"""Claims Execution work and routes it to a mode-specific Runner."""

from uuid import UUID

from opentelemetry.trace import SpanKind
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.config import Settings
from executor_service.domain.enums import OperationMode, RuntimePool
from executor_service.domain.results import ExecutionResultStore
from executor_service.infrastructure.artifacts import ExecutionArtifactManager
from executor_service.infrastructure.db.models import ExecutionORM
from executor_service.infrastructure.execution_worker.claiming import (
    ExecutionClaimer,
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
from executor_service.infrastructure.workspace import WorkspaceManager
from executor_service.tracing import TracingManager


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
        tracing: TracingManager,
    ) -> None:
        self._session_factory = session_factory
        self._claimer = claimer
        self._tracing = tracing
        self._finalizer = ExecutionRunFinalizer(session_factory, settings)
        operation_state = MultiOperationState(session_factory)
        self._single = SingleExecutionRunner(
            settings,
            artifacts,
            result_store,
            workspace,
            claimer,
            lease_heartbeat,
            notebook_projector,
            step_executor,
            self._finalizer,
            driver_provider,
            tracing,
        )
        self._multi = MultiExecutionRunner(
            artifacts,
            result_store,
            workspace,
            lease_heartbeat,
            notebook_projector,
            step_executor,
            self._finalizer,
            operation_state,
            driver_provider,
            tracing,
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
            claimed = await self._claimer.claim(execution_id)
            if claimed is None:
                return
            execution, target, lease = claimed
            if execution.operation_mode == OperationMode.MULTI:
                await self._multi.run(execution, target, lease)
            else:
                await self._single.run(execution, target, lease)

    async def _execution_pool(self, execution_id: UUID) -> RuntimePool | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(ExecutionORM.runtime_pool).where(
                    ExecutionORM.id == execution_id
                )
            )
