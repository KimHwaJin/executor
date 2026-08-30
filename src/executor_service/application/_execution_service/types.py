"""Shared types for internal Execution command handlers."""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from executor_service.domain.models import Execution
from executor_service.domain.ports import UnitOfWork

UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True, slots=True)
class ExecutionCommandResult:
    execution: Execution
    operation_id: UUID
