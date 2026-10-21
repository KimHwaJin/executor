"""ExecutionService and the lifecycle use cases behind it."""

from collections.abc import Mapping
from uuid import UUID

from executor_service.application.commands import (
    CancelExecutionCommand,
    CreateOperationCommand,
    FinalizeExecutionCommand,
    RetryExecutionCommand,
    SubmitExecutionCommand,
)
from executor_service.application.services.lifecycle import (
    ExecutionLifecycleCommands,
)
from executor_service.application.services.operations import (
    ExecutionOperationCommands,
)
from executor_service.application.services.submission import (
    ExecutionSubmissionCommands,
)
from executor_service.application.services.support import (
    ExecutionCommandSupport,
)
from executor_service.application.services.types import (
    ExecutionCommandResult,
    UnitOfWorkFactory,
)
from executor_service.domain.enums import RuntimeType
from executor_service.domain.models import Execution
from executor_service.domain.results import ExecutionResultStore


class ExecutionService:
    """Application entry point for Execution state changes."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        runtime_profiles: Mapping[RuntimeType, tuple[str, ...]],
        result_store: ExecutionResultStore,
        *,
        max_steps_per_operation: int = 100,
        max_steps_per_execution: int = 1000,
    ) -> None:
        support = ExecutionCommandSupport(
            uow_factory,
            result_store,
            max_steps_per_operation=max_steps_per_operation,
            max_steps_per_execution=max_steps_per_execution,
        )
        self._submission = ExecutionSubmissionCommands(
            support, runtime_profiles
        )
        self._operations = ExecutionOperationCommands(support)
        self._lifecycle = ExecutionLifecycleCommands(support)

    @property
    def runtime_profiles(self) -> dict[str, tuple[str, ...]]:
        return self._submission.runtime_profiles

    async def submit(self, command: SubmitExecutionCommand) -> Execution:
        return (await self.submit_result(command)).execution

    async def submit_result(
        self, command: SubmitExecutionCommand
    ) -> ExecutionCommandResult:
        return await self._submission.submit(command)

    async def create_operation(
        self, command: CreateOperationCommand
    ) -> Execution:
        return (await self.create_operation_result(command)).execution

    async def create_operation_result(
        self, command: CreateOperationCommand
    ) -> ExecutionCommandResult:
        return await self._operations.create(command)

    async def finalize_execution(
        self, command: FinalizeExecutionCommand
    ) -> Execution:
        return await self._lifecycle.finalize(command)

    async def get(self, execution_id: UUID) -> Execution:
        return await self._lifecycle.get(execution_id)

    async def cancel(self, command: CancelExecutionCommand) -> Execution:
        return await self._lifecycle.cancel(command)

    async def retry(self, command: RetryExecutionCommand) -> Execution:
        return (await self.retry_result(command)).execution

    async def retry_result(
        self, command: RetryExecutionCommand
    ) -> ExecutionCommandResult:
        return await self._lifecycle.retry(command)


__all__ = [
    "ExecutionCommandResult",
    "ExecutionCommandSupport",
    "ExecutionLifecycleCommands",
    "ExecutionOperationCommands",
    "ExecutionService",
    "ExecutionSubmissionCommands",
    "UnitOfWorkFactory",
]
