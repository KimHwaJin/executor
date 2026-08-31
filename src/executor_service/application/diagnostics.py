"""Application read port for immutable execution diagnostic history."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from executor_service.application.pagination import Page
from executor_service.domain.diagnostics import RuntimeDiagnostic
from executor_service.domain.enums import ActorType


@dataclass(frozen=True, slots=True)
class DiagnosticView:
    id: UUID
    execution_id: UUID
    attempt_id: UUID | None
    operation_id: UUID | None
    step_id: UUID | None
    step_sequence: int | None
    fencing_token: int
    diagnostic: RuntimeDiagnostic
    occurred_at: datetime
    created_at: datetime
    updated_at: datetime
    created_by_type: ActorType | None
    created_by: str | None
    updated_by_type: ActorType | None
    updated_by: str | None


class DiagnosticQueryService(Protocol):
    async def list(
        self,
        execution_id: UUID,
        *,
        attempt_id: UUID | None = None,
        operation_id: UUID | None = None,
        step_id: UUID | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> Page[DiagnosticView]: ...
