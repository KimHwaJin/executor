"""Internal immutable types for Maintenance Run processing."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class MaintenanceRunLease:
    run_id: UUID
    owner: str
    fencing_token: int
    actor_client: str | None
    actor_id: str | None
