"""Execution Event transport contracts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from executor_service.application.execution_queries import ExecutionEventView
from executor_service.application.pagination import Page
from executor_service.domain.enums import OutboxStatus
from executor_service.interfaces._contracts.common import (
    AuditFields,
    ContractModel,
    PageResponse,
)


class EventDelivery(ContractModel):
    status: OutboxStatus
    attempt_count: int
    available_at: datetime
    published_at: datetime | None
    last_error: str | None


class ExecutionEventResponse(AuditFields):
    event_id: UUID
    execution_id: UUID
    event_sequence: int
    schema_version: str
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime
    delivery: EventDelivery | None

    @classmethod
    def from_view(
        cls,
        view: ExecutionEventView,
    ) -> "ExecutionEventResponse":
        return cls(
            event_id=view.id,
            execution_id=view.execution_id,
            event_sequence=view.event_sequence,
            schema_version=view.schema_version,
            event_type=view.event_type,
            payload=view.payload,
            occurred_at=view.created_at,
            delivery=(
                EventDelivery(
                    status=view.delivery_status,
                    attempt_count=view.publish_attempt_count,
                    available_at=view.available_at,
                    published_at=view.published_at,
                    last_error=view.last_error,
                )
                if view.delivery_status is not None
                and view.publish_attempt_count is not None
                and view.available_at is not None
                else None
            ),
            created_by_type=view.created_by_type,
            created_by=view.created_by,
            updated_by_type=view.updated_by_type,
            updated_by=view.updated_by,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class ExecutionEventPageResponse(PageResponse):
    items: list[ExecutionEventResponse]

    @classmethod
    def from_page(
        cls,
        page: Page[ExecutionEventView],
    ) -> "ExecutionEventPageResponse":
        return cls(
            items=[
                ExecutionEventResponse.from_view(item) for item in page.items
            ],
            next_cursor=page.next_cursor,
            has_more=page.next_cursor is not None,
        )
