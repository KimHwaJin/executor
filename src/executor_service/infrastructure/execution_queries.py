"""Public SQLAlchemy facade for execution history queries."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from executor_service.application.execution_queries import (
    ExecutionArtifactView,
    ExecutionAttemptView,
    ExecutionDetailView,
    ExecutionEventView,
    ExecutionOperationView,
    ExecutionResultSnapshot,
    ExecutionStepAttemptView,
    ExecutionSummaryView,
    OperationResultSnapshot,
)
from executor_service.application.pagination import Page
from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.models import ExecutionStep
from executor_service.infrastructure._execution_queries import (
    SQLAlchemyArtifactReader,
    SQLAlchemyAttemptReader,
    SQLAlchemyEventReader,
    SQLAlchemyExecutionReader,
    SQLAlchemyOperationReader,
    SQLAlchemyResultSnapshotReader,
)


class SQLAlchemyExecutionQueryService:
    """Stable facade composed from responsibility-specific readers."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._executions = SQLAlchemyExecutionReader(session_factory)
        self._attempts = SQLAlchemyAttemptReader(session_factory)
        self._operations = SQLAlchemyOperationReader(session_factory)
        self._events = SQLAlchemyEventReader(session_factory)
        self._artifacts = SQLAlchemyArtifactReader(session_factory)
        self._results = SQLAlchemyResultSnapshotReader(session_factory)

    async def executions(
        self,
        *,
        user_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        workflow_id: str | None = None,
        status: ExecutionStatus | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionSummaryView]:
        return await self._executions.executions(
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            workflow_id=workflow_id,
            status=status,
            cursor=cursor,
            limit=limit,
        )

    async def execution(self, execution_id: UUID) -> ExecutionDetailView:
        return await self._executions.execution(execution_id)

    async def steps(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionStep]:
        return await self._executions.steps(
            execution_id,
            cursor=cursor,
            limit=limit,
        )

    async def step(self, execution_id: UUID, step_id: UUID) -> ExecutionStep:
        return await self._executions.step(execution_id, step_id)

    async def attempts(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionAttemptView]:
        return await self._attempts.attempts(
            execution_id,
            cursor=cursor,
            limit=limit,
        )

    async def attempt(
        self, execution_id: UUID, attempt_id: UUID
    ) -> ExecutionAttemptView:
        return await self._attempts.attempt(execution_id, attempt_id)

    async def operations(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionOperationView]:
        return await self._operations.operations(
            execution_id,
            cursor=cursor,
            limit=limit,
        )

    async def operation(
        self, execution_id: UUID, operation_id: UUID
    ) -> ExecutionOperationView:
        return await self._operations.operation(execution_id, operation_id)

    async def operation_steps(
        self,
        execution_id: UUID,
        operation_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionStep]:
        return await self._operations.operation_steps(
            execution_id,
            operation_id,
            cursor=cursor,
            limit=limit,
        )

    async def attempt_steps(
        self,
        execution_id: UUID,
        attempt_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionStepAttemptView]:
        return await self._attempts.attempt_steps(
            execution_id,
            attempt_id,
            cursor=cursor,
            limit=limit,
        )

    async def events(
        self,
        execution_id: UUID,
        *,
        after_sequence: int = 0,
        cursor: str | None = None,
        limit: int = 200,
    ) -> Page[ExecutionEventView]:
        return await self._events.events(
            execution_id,
            after_sequence=after_sequence,
            cursor=cursor,
            limit=limit,
        )

    async def artifacts(
        self,
        execution_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[ExecutionArtifactView]:
        return await self._artifacts.artifacts(
            execution_id,
            cursor=cursor,
            limit=limit,
        )

    async def artifact(self, artifact_id: UUID) -> ExecutionArtifactView:
        return await self._artifacts.artifact(artifact_id)

    async def operation_result_snapshot(
        self, execution_id: UUID, operation_id: UUID
    ) -> OperationResultSnapshot:
        return await self._results.operation_result_snapshot(
            execution_id, operation_id
        )

    async def execution_result_snapshot(
        self, execution_id: UUID
    ) -> ExecutionResultSnapshot:
        return await self._results.execution_result_snapshot(execution_id)
