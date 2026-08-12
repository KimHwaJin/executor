"""REST administration facade for persistent Runtime Targets."""

from collections.abc import Awaitable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query

from executor_service.container import ApplicationContainer
from executor_service.domain.enums import RuntimePool, RuntimeTargetStatus, RuntimeType
from executor_service.interfaces.http.schemas import (
    ErrorResponse,
    RuntimePoolPageResponse,
    RuntimePoolResponse,
    RuntimeTargetMutationRequest,
    RuntimeTargetPageResponse,
    RuntimeTargetProbeRequest,
    RuntimeTargetPurgeRequest,
    RuntimeTargetPurgeResponse,
    RuntimeTargetResponse,
    RuntimeTargetUpsertRequest,
)
from executor_service.tracing import TracingManager

FleetLimit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=2048)]

FLEET_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Invalid fleet configuration"},
    404: {"model": ErrorResponse, "description": "Runtime Target not found"},
    409: {
        "model": ErrorResponse,
        "description": "Idempotency or hard-purge safety conflict",
    },
    422: {"model": ErrorResponse, "description": "Invalid request or cursor"},
}


async def _trace_call[T](
    tracing: TracingManager,
    name: str,
    operation: Awaitable[T],
    attributes: dict[str, object] | None = None,
) -> T:
    with tracing.span(name, attributes=attributes):
        return await operation


def build_runtime_target_router(container: ApplicationContainer) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["runtime-targets"])
    registry = container.runtime_registry
    tracing = container.tracing

    @router.post(
        "/runtime-targets",
        response_model=RuntimeTargetResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Register or update and immediately probe a Runtime Target",
    )
    async def upsert_runtime_target(
        request: RuntimeTargetUpsertRequest,
    ) -> RuntimeTargetResponse:
        view = await _trace_call(
            tracing,
            "executor.http.runtime_target_upsert",
            registry.upsert(request.to_command()),
            {"executor.runtime.target.name": request.name},
        )
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
        target_status: Annotated[RuntimeTargetStatus | None, Query(alias="status")] = None,
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
        view = await _trace_call(
            tracing,
            "executor.http.runtime_target_get",
            registry.get(target_id),
            {"executor.runtime.target.id": str(target_id)},
        )
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
        view = await _trace_call(
            tracing,
            "executor.http.runtime_target_probe",
            registry.probe(
                target_id,
                actor_type=request.actor.type,
                actor_id=request.actor.id,
            ),
            {"executor.runtime.target.id": str(target_id)},
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
        view = await _trace_call(
            tracing,
            "executor.http.runtime_target_drain",
            registry.set_state(request.to_state_command(target_id, RuntimeTargetStatus.DRAINING)),
            {"executor.runtime.target.id": str(target_id)},
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
        view = await _trace_call(
            tracing,
            "executor.http.runtime_target_activate",
            registry.set_state(request.to_state_command(target_id, RuntimeTargetStatus.ACTIVE)),
            {"executor.runtime.target.id": str(target_id)},
        )
        return RuntimeTargetResponse.from_view(view)

    @router.delete(
        "/runtime-targets/{target_id}",
        response_model=RuntimeTargetResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Soft-delete a target while preserving execution history",
    )
    async def remove_runtime_target(
        target_id: UUID, request: RuntimeTargetMutationRequest
    ) -> RuntimeTargetResponse:
        view = await _trace_call(
            tracing,
            "executor.http.runtime_target_remove",
            registry.remove(request.to_remove_command(target_id)),
            {"executor.runtime.target.id": str(target_id)},
        )
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
        view = await _trace_call(
            tracing,
            "executor.http.runtime_target_purge",
            registry.purge(request.to_command(target_id)),
            {"executor.runtime.target.id": str(target_id)},
        )
        return RuntimeTargetPurgeResponse.from_view(view)

    return router
