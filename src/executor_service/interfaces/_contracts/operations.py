"""Execution Operation transport contracts."""

from typing import Any
from uuid import UUID

from executor_service.application.execution_queries import (
    ExecutionOperationView,
)
from executor_service.application.pagination import Page
from executor_service.domain.enums import OperationStatus
from executor_service.interfaces._contracts.common import (
    AuditFields,
    ContractModel,
    Lifecycle,
    PageResponse,
)


class OperationSequenceRange(ContractModel):
    first: int
    last: int


class OperationResult(ContractModel):
    status: OperationStatus
    error_message: str | None


class ExecutionOperationResponse(AuditFields):
    operation_id: UUID
    execution_id: UUID
    operation_number: int
    schema_version: str
    sequence_range: OperationSequenceRange
    operation_timeout_seconds: int | None
    metadata: dict[str, Any]
    execution_attempt_id: UUID | None
    result: OperationResult
    lifecycle: Lifecycle
    step_count: int

    @classmethod
    def from_view(
        cls,
        view: ExecutionOperationView,
    ) -> "ExecutionOperationResponse":
        return cls(
            operation_id=view.id,
            execution_id=view.execution_id,
            operation_number=view.operation_number,
            schema_version=view.schema_version,
            sequence_range=OperationSequenceRange(
                first=view.first_sequence,
                last=view.last_sequence,
            ),
            operation_timeout_seconds=view.operation_timeout_seconds,
            metadata=view.metadata,
            execution_attempt_id=view.execution_attempt_id,
            result=OperationResult(
                status=view.status,
                error_message=view.error_message,
            ),
            lifecycle=Lifecycle(
                started_at=view.started_at,
                finished_at=view.finished_at,
            ),
            step_count=view.step_count,
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ExecutionOperationSummaryResponse(AuditFields):
    operation_id: UUID
    operation_number: int
    sequence_range: OperationSequenceRange
    result: OperationResult
    lifecycle: Lifecycle
    step_count: int

    @classmethod
    def from_view(
        cls,
        view: ExecutionOperationView,
    ) -> "ExecutionOperationSummaryResponse":
        return cls(
            operation_id=view.id,
            operation_number=view.operation_number,
            sequence_range=OperationSequenceRange(
                first=view.first_sequence,
                last=view.last_sequence,
            ),
            result=OperationResult(
                status=view.status,
                error_message=view.error_message,
            ),
            lifecycle=Lifecycle(
                started_at=view.started_at,
                finished_at=view.finished_at,
            ),
            step_count=view.step_count,
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ExecutionOperationPageResponse(PageResponse):
    items: list[ExecutionOperationSummaryResponse]

    @classmethod
    def from_page(
        cls,
        page: Page[ExecutionOperationView],
    ) -> "ExecutionOperationPageResponse":
        return cls(
            items=[
                ExecutionOperationSummaryResponse.from_view(item)
                for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )
