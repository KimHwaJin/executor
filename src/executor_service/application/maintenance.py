"""Application contracts for Executor-wide maintenance admission."""

from dataclasses import dataclass
from datetime import datetime

from executor_service.domain.enums import ActorType, ExecutorAdmissionState


@dataclass(frozen=True, slots=True)
class SetExecutorAdmissionCommand:
    idempotency_key: str
    desired_state: ExecutorAdmissionState
    actor_type: ActorType
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
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
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
