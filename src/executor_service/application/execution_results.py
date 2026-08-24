"""Consolidated result reads used after an Executor event wakes an Agent."""

from dataclasses import dataclass
from uuid import UUID

from executor_service.application.execution_queries import (
    ExecutionArtifactView,
    ExecutionAttemptView,
    ExecutionDetailView,
    ExecutionOperationView,
    ExecutionQueryService,
    ExecutionStepAttemptView,
)
from executor_service.domain.models import ExecutionStep


@dataclass(frozen=True, slots=True)
class OperationResultBundle:
    operation: ExecutionOperationView
    steps: tuple[ExecutionStep, ...]


@dataclass(frozen=True, slots=True)
class AttemptResultBundle:
    attempt: ExecutionAttemptView
    steps: tuple[ExecutionStepAttemptView, ...]


@dataclass(frozen=True, slots=True)
class ExecutionResultBundle:
    execution: ExecutionDetailView
    operations: tuple[OperationResultBundle, ...]
    attempts: tuple[AttemptResultBundle, ...]
    artifacts: tuple[ExecutionArtifactView, ...]


class ExecutionResultQueryService:
    def __init__(self, queries: ExecutionQueryService) -> None:
        self._queries = queries

    async def operation(
        self, execution_id: UUID, operation_id: UUID
    ) -> OperationResultBundle:
        operation = await self._queries.operation(execution_id, operation_id)
        steps = await self._all_operation_steps(execution_id, operation_id)
        return OperationResultBundle(operation=operation, steps=tuple(steps))

    async def execution(self, execution_id: UUID) -> ExecutionResultBundle:
        execution = await self._queries.execution(execution_id)
        operations = []
        for operation in await self._all_operations(execution_id):
            operations.append(
                OperationResultBundle(
                    operation=operation,
                    steps=tuple(
                        await self._all_operation_steps(
                            execution_id, operation.id
                        )
                    ),
                )
            )
        attempts = []
        for attempt in await self._all_attempts(execution_id):
            attempts.append(
                AttemptResultBundle(
                    attempt=attempt,
                    steps=tuple(
                        await self._all_attempt_steps(execution_id, attempt.id)
                    ),
                )
            )
        return ExecutionResultBundle(
            execution=execution,
            operations=tuple(operations),
            attempts=tuple(attempts),
            artifacts=tuple(await self._all_artifacts(execution_id)),
        )

    async def _all_operations(
        self, execution_id: UUID
    ) -> list[ExecutionOperationView]:
        items: list[ExecutionOperationView] = []
        cursor = None
        while True:
            page = await self._queries.operations(
                execution_id, cursor=cursor, limit=200
            )
            items.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                return items

    async def _all_operation_steps(
        self, execution_id: UUID, operation_id: UUID
    ) -> list[ExecutionStep]:
        items: list[ExecutionStep] = []
        cursor = None
        while True:
            page = await self._queries.operation_steps(
                execution_id, operation_id, cursor=cursor, limit=200
            )
            items.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                return items

    async def _all_attempts(
        self, execution_id: UUID
    ) -> list[ExecutionAttemptView]:
        items: list[ExecutionAttemptView] = []
        cursor = None
        while True:
            page = await self._queries.attempts(
                execution_id, cursor=cursor, limit=200
            )
            items.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                return items

    async def _all_attempt_steps(
        self, execution_id: UUID, attempt_id: UUID
    ) -> list[ExecutionStepAttemptView]:
        items: list[ExecutionStepAttemptView] = []
        cursor = None
        while True:
            page = await self._queries.attempt_steps(
                execution_id, attempt_id, cursor=cursor, limit=200
            )
            items.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                return items

    async def _all_artifacts(
        self, execution_id: UUID
    ) -> list[ExecutionArtifactView]:
        items: list[ExecutionArtifactView] = []
        cursor = None
        while True:
            page = await self._queries.artifacts(
                execution_id, cursor=cursor, limit=1000
            )
            items.extend(page.items)
            cursor = page.next_cursor
            if cursor is None:
                return items
