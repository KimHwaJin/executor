"""REST administration facade for persistent Runtime Targets."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from executor_service.application.runtime_targets import (
    PurgeRuntimeTargetCommand,
)
from executor_service.container import ApplicationContainer
from executor_service.domain.enums import (
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
)
from executor_service.interfaces.auth import current_caller, require_user
from executor_service.interfaces.contracts import (
    RuntimePoolPageResponse,
    RuntimePoolResponse,
    RuntimeTargetPageResponse,
    RuntimeTargetResponse,
    RuntimeTargetUpsertRequest,
)
from executor_service.interfaces.http.identity import documented_admin_key
from executor_service.interfaces.http.schemas import (
    ErrorResponse,
    RuntimeTargetMutationRequest,
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
    router = APIRouter(
        prefix="/api/v1",
        tags=["runtime-targets"],
        dependencies=[Depends(documented_admin_key)],
    )
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
        view = await registry.upsert(request.to_command(current_caller()))
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
        target_id: UUID,
    ) -> RuntimeTargetResponse:
        caller = current_caller()
        view = await registry.probe(
            target_id,
            actor_client=caller.client,
            actor_id=require_user(caller),
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
            request.to_state_command(
                target_id, RuntimeTargetStatus.DRAINING, current_caller()
            )
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
            request.to_state_command(
                target_id, RuntimeTargetStatus.ACTIVE, current_caller()
            )
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
        view = await registry.disable(
            request.to_disable_command(target_id, current_caller())
        )
        return RuntimeTargetResponse.from_view(view)

    @router.post(
        "/runtime-targets/{target_id}/purge",
        response_model=RuntimeTargetPurgeResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Remove a disabled target while preserving execution history",
    )
    async def purge_runtime_target(
        target_id: UUID,
    ) -> RuntimeTargetPurgeResponse:
        caller = current_caller()
        view = await registry.purge(
            PurgeRuntimeTargetCommand(
                target_id=target_id,
                actor_client=caller.client,
                actor_id=require_user(caller),
            )
        )
        return RuntimeTargetPurgeResponse.from_view(view)

    return router
