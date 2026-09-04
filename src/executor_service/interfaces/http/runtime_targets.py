"""REST administration facade for persistent Runtime Targets."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query

from executor_service.container import ApplicationContainer
from executor_service.domain.enums import (
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
)
from executor_service.interfaces.contracts import (
    RuntimePoolPageResponse,
    RuntimePoolResponse,
    RuntimeTargetPageResponse,
    RuntimeTargetResponse,
    RuntimeTargetUpsertRequest,
)
from executor_service.interfaces.http.schemas import (
    ErrorResponse,
    RuntimeTargetMutationRequest,
    RuntimeTargetProbeRequest,
    RuntimeTargetPurgeRequest,
    RuntimeTargetPurgeResponse,
)

FleetLimit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=2048)]

FLEET_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {
        "model": ErrorResponse,
        "description": "Invalid fleet configuration",
    },
    404: {"model": ErrorResponse, "description": "Runtime Target not found"},
    409: {
        "model": ErrorResponse,
        "description": "Idempotency or hard-purge safety conflict",
    },
    422: {"model": ErrorResponse, "description": "Invalid request or cursor"},
}


def build_runtime_target_router(container: ApplicationContainer) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["runtime-targets"])
    registry = container.runtime_registry

    @router.post(
        "/runtime-targets",
        response_model=RuntimeTargetResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Register or update and immediately probe a Runtime Target",
    )
    async def upsert_runtime_target(
        request: RuntimeTargetUpsertRequest,
    ) -> RuntimeTargetResponse:
        view = await registry.upsert(request.to_command())
        return RuntimeTargetResponse.from_view(view)

    @router.get(
        "/runtime-targets",
        response_model=RuntimeTargetPageResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="List Runtime Targets and current capacity",
    )
    async def list_runtime_targets(
        pool: RuntimePool | None = None,
        runtime_type: RuntimeType | None = None,
        target_status: Annotated[
            RuntimeTargetStatus | None, Query(alias="status")
        ] = None,
        enabled: bool | None = None,
        cursor: Cursor = None,
        limit: FleetLimit = 100,
    ) -> RuntimeTargetPageResponse:
        page = await registry.list(
            pool,
            runtime_type=runtime_type,
            status=target_status,
            enabled=enabled,
            cursor=cursor,
            limit=limit,
        )
        return RuntimeTargetPageResponse.from_page(page)

    @router.get(
        "/runtime-pools",
        response_model=RuntimePoolPageResponse,
        summary="Get capacity and health summaries for all Runtime Pools",
    )
    async def list_runtime_pools() -> RuntimePoolPageResponse:
        views = await registry.pool_summaries()
        return RuntimePoolPageResponse(
            items=[RuntimePoolResponse.from_view(view) for view in views]
        )

    @router.get(
        "/runtime-targets/{target_id}",
        response_model=RuntimeTargetResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Get one Runtime Target without exposing its credential",
    )
    async def get_runtime_target(target_id: UUID) -> RuntimeTargetResponse:
        view = await registry.get(target_id)
        return RuntimeTargetResponse.from_view(view)

    @router.post(
        "/runtime-targets/{target_id}/probe",
        response_model=RuntimeTargetResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Probe one enabled Runtime Target now",
    )
    async def probe_runtime_target(
        target_id: UUID, request: RuntimeTargetProbeRequest
    ) -> RuntimeTargetResponse:
        view = await registry.probe(
            target_id,
            actor_type=request.actor.type,
            actor_id=request.actor.id,
        )
        return RuntimeTargetResponse.from_view(view)

    @router.post(
        "/runtime-targets/{target_id}/drain",
        response_model=RuntimeTargetResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Stop assigning new work while current work finishes",
    )
    async def drain_runtime_target(
        target_id: UUID, request: RuntimeTargetMutationRequest
    ) -> RuntimeTargetResponse:
        view = await registry.set_state(
            request.to_state_command(target_id, RuntimeTargetStatus.DRAINING)
        )
        return RuntimeTargetResponse.from_view(view)

    @router.post(
        "/runtime-targets/{target_id}/activate",
        response_model=RuntimeTargetResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Enable, probe, and return a healthy target to active scheduling",
    )
    async def activate_runtime_target(
        target_id: UUID, request: RuntimeTargetMutationRequest
    ) -> RuntimeTargetResponse:
        view = await registry.set_state(
            request.to_state_command(target_id, RuntimeTargetStatus.ACTIVE)
        )
        return RuntimeTargetResponse.from_view(view)

    @router.post(
        "/runtime-targets/{target_id}/disable",
        response_model=RuntimeTargetResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Disable a target while preserving execution history",
    )
    async def disable_runtime_target(
        target_id: UUID, request: RuntimeTargetMutationRequest
    ) -> RuntimeTargetResponse:
        view = await registry.disable(request.to_disable_command(target_id))
        return RuntimeTargetResponse.from_view(view)

    @router.post(
        "/runtime-targets/{target_id}/purge",
        response_model=RuntimeTargetPurgeResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Permanently remove an unused, already disabled target",
    )
    async def purge_runtime_target(
        target_id: UUID, request: RuntimeTargetPurgeRequest
    ) -> RuntimeTargetPurgeResponse:
        view = await registry.purge(request.to_command(target_id))
        return RuntimeTargetPurgeResponse.from_view(view)

    return router
