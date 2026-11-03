"""Application contracts for Executor-wide maintenance admission."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from executor_service.domain.enums import (
    ExecutorAdmissionState,
    MaintenanceRunAction,
    MaintenanceRunStatus,
)


@dataclass(frozen=True, slots=True)
class SetExecutorAdmissionCommand:
    idempotency_key: str
    desired_state: ExecutorAdmissionState
    actor_client: str
    actor_id: str


@dataclass(frozen=True, slots=True)
class ExecutorMaintenanceView:
    admission_state: ExecutorAdmissionState
    version: int
    queued_execution_count: int
    active_execution_count: int
    cancel_requested_count: int
    unresolved_cleanup_count: int
    active_runtime_session_count: int
    active_run_id: UUID | None
    active_run_action: MaintenanceRunAction | None
    active_run_status: MaintenanceRunStatus | None
    created_by_client: str | None
    created_by: str | None
    updated_by_client: str | None
    updated_by: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def accepting_new_executions(self) -> bool:
        return self.admission_state == ExecutorAdmissionState.ACTIVE

    @property
    def safe_to_shutdown(self) -> bool:
        return (
            self.active_execution_count == 0
            and self.cancel_requested_count == 0
            and self.unresolved_cleanup_count == 0
            and self.active_runtime_session_count == 0
        )
