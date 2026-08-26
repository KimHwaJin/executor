"""Application contracts for durable Executor Maintenance Runs."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from executor_service.domain.enums import (
    ActorType,
    ExecutionStatus,
    MaintenanceRunAction,
    MaintenanceRunStatus,
    MaintenanceRunTargetStatus,
)


@dataclass(frozen=True, slots=True)
class CreateMaintenanceRunCommand:
    idempotency_key: str
    action: MaintenanceRunAction
    actor_type: ActorType
    actor_id: str


@dataclass(frozen=True, slots=True)
class MaintenanceRunCounts:
    total: int
    pending: int
    stop_requested: int
    stopped: int
    failed: int

    @property
    def remaining(self) -> int:
        return self.pending + self.stop_requested


@dataclass(frozen=True, slots=True)
class MaintenanceRunView:
    id: UUID
    action: MaintenanceRunAction
    status: MaintenanceRunStatus
    counts: MaintenanceRunCounts
    error_message: str | None
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class MaintenanceRunTargetView:
    id: UUID
    maintenance_run_id: UUID
    execution_id: UUID
    selected_execution_status: ExecutionStatus
    status: MaintenanceRunTargetStatus
    error_message: str | None
    stop_requested_at: datetime | None
    completed_at: datetime | None
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime
