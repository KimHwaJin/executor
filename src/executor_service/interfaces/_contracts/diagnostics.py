"""Transport-neutral diagnostic history response, bounded and paginated."""

from dataclasses import asdict
from datetime import datetime
from typing import Literal
from uuid import UUID

from executor_service.application.diagnostics import DiagnosticView
from executor_service.application.pagination import Page
from executor_service.domain.diagnostics import (
    DiagnosticCategory,
    DiagnosticOrigin,
)
from executor_service.interfaces._contracts.common import (
    AuditFields,
    ContractModel,
    PageResponse,
)


class DiagnosticCauseResponse(ContractModel):
    exception_type: str
    message: str
    errno: int | None


class RuntimeDiagnosticResponse(ContractModel):
    code: str
    phase: str
    category: DiagnosticCategory
    origin: DiagnosticOrigin
    severity: Literal["ERROR"]
    message: str
    causes: list[DiagnosticCauseResponse]
    causes_truncated: bool


class ExecutionDiagnosticResponse(AuditFields):
    id: UUID
    execution_id: UUID
    attempt_id: UUID | None
    operation_id: UUID | None
    step_id: UUID | None
    step_sequence: int | None
    fencing_token: int
    occurred_at: datetime
    diagnostic: RuntimeDiagnosticResponse


class ExecutionDiagnosticPageResponse(PageResponse):
    items: list[ExecutionDiagnosticResponse]

    @classmethod
    def from_page(
        cls, page: Page[DiagnosticView]
    ) -> "ExecutionDiagnosticPageResponse":
        return cls(
            items=[
                ExecutionDiagnosticResponse.model_validate(asdict(item))
                for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )
