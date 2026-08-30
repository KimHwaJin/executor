"""Internal immutable types for Maintenance Run processing."""

from dataclasses import dataclass
from uuid import UUID

from executor_service.domain.enums import ActorType


@dataclass(frozen=True, slots=True)
class MaintenanceRunLease:
    run_id: UUID
    owner: str
    fencing_token: int
    actor_type: ActorType | None
    actor_id: str | None
