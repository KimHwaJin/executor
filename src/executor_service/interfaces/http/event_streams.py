"""REST administration facade for inspecting the Redis event Streams."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query

from executor_service.container import ApplicationContainer
from executor_service.domain.tenants import TENANT_ID_PATTERN
from executor_service.interfaces.contracts import (
    EventStreamEntryPageResponse,
    EventStreamOverviewResponse,
    EventStreamResponse,
    PendingEntryPageResponse,
)
from executor_service.interfaces.http.identity import documented_admin_key
from executor_service.interfaces.http.schemas import ErrorResponse

TenantId = Annotated[str, Path(pattern=TENANT_ID_PATTERN)]
StreamId = Annotated[str | None, Query(pattern=r"^\d{1,20}(-\d{1,20})?$")]

UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    503: {"model": ErrorResponse, "description": "Redis unavailable"},
}
STREAM_READ_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **UNAVAILABLE,
    404: {"model": ErrorResponse, "description": "Stream or group not found"},
    422: {"model": ErrorResponse, "description": "Invalid request"},
}


def build_event_stream_router(container: ApplicationContainer) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["event-streams"],
        dependencies=[Depends(documented_admin_key)],
    )
    inspector = container.event_streams

    @router.get(
        "/event-streams",
        response_model=EventStreamOverviewResponse,
        responses=UNAVAILABLE,
        summary="Show the Redis connection and every tenant's event Stream",
    )
    async def get_event_streams() -> EventStreamOverviewResponse:
        return EventStreamOverviewResponse.model_validate(
            await inspector.overview()
        )

    @router.get(
        "/event-streams/{tenant_id}",
        response_model=EventStreamResponse,
        responses=STREAM_READ_ERROR_RESPONSES,
        summary="Show one tenant's event Stream and its consumer groups",
    )
    async def get_event_stream(tenant_id: TenantId) -> EventStreamResponse:
        return EventStreamResponse.model_validate(
            await inspector.stream(tenant_id)
        )

    @router.get(
        "/event-streams/{tenant_id}/entries",
        response_model=EventStreamEntryPageResponse,
        responses=STREAM_READ_ERROR_RESPONSES,
        summary="List a Stream's entries, newest first",
        description=(
            "A filtered request reads at most 1000 entries, so it may return "
            "fewer than `limit` items and still a `next_before` to go on."
        ),
    )
    async def list_event_stream_entries(
        tenant_id: TenantId,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before: StreamId = None,
        execution_id: Annotated[str | None, Query(max_length=64)] = None,
        event_type: Annotated[str | None, Query(max_length=128)] = None,
    ) -> EventStreamEntryPageResponse:
        return EventStreamEntryPageResponse.model_validate(
            await inspector.entries(
                tenant_id,
                limit=limit,
                before=before,
                execution_id=execution_id,
                event_type=event_type,
            )
        )

    @router.get(
        "/event-streams/{tenant_id}/groups/{group}/pending",
        response_model=PendingEntryPageResponse,
        responses=STREAM_READ_ERROR_RESPONSES,
        summary="List entries a consumer group has not acknowledged",
    )
    async def list_pending_entries(
        tenant_id: TenantId,
        group: Annotated[str, Path(min_length=1, max_length=255)],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        consumer: Annotated[str | None, Query(max_length=255)] = None,
        min_idle_ms: Annotated[int, Query(ge=0)] = 0,
    ) -> PendingEntryPageResponse:
        return PendingEntryPageResponse.model_validate(
            await inspector.pending(
                tenant_id,
                group,
                limit=limit,
                consumer=consumer,
                min_idle_ms=min_idle_ms,
            )
        )

    return router
