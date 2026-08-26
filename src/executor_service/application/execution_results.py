"""Consolidated result reads used after an Executor event wakes an Agent."""

from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

from executor_service.application.execution_queries import (
    ExecutionArtifactView,
    ExecutionAttemptView,
    ExecutionDetailView,
    ExecutionOperationView,
    ExecutionQueryService,
)
from executor_service.domain.models import ExecutionStep


@dataclass(frozen=True, slots=True)
class OperationResultBundle:
    execution: ExecutionDetailView
    operation: ExecutionOperationView
    steps: tuple[ExecutionStep, ...]


@dataclass(frozen=True, slots=True)
class ExecutionResultBundle:
    execution: ExecutionDetailView
    operations: tuple[OperationResultBundle, ...]
    attempts: tuple[ExecutionAttemptView, ...]
    artifacts: tuple[ExecutionArtifactView, ...]


class ExecutionResultQueryService:
    def __init__(self, queries: ExecutionQueryService) -> None:
        self._queries = queries

    async def operation(
        self, execution_id: UUID, operation_id: UUID
    ) -> OperationResultBundle:
        snapshot = await self._queries.operation_result_snapshot(
            execution_id, operation_id
        )
        return OperationResultBundle(
            execution=snapshot.execution,
            operation=snapshot.operation,
            steps=snapshot.steps,
        )

    async def execution(self, execution_id: UUID) -> ExecutionResultBundle:
        snapshot = await self._queries.execution_result_snapshot(execution_id)
        steps_by_operation: dict[UUID, list[ExecutionStep]] = defaultdict(list)
        for step in snapshot.steps:
            if step.operation_id is None:
                raise RuntimeError(
                    "Persisted Execution Step has no Operation."
                )
            steps_by_operation[step.operation_id].append(step)
        return ExecutionResultBundle(
            execution=snapshot.execution,
            operations=tuple(
                OperationResultBundle(
                    execution=snapshot.execution,
                    operation=operation,
                    steps=tuple(steps_by_operation[operation.id]),
                )
                for operation in snapshot.operations
            ),
            attempts=snapshot.attempts,
            artifacts=snapshot.artifacts,
        )
