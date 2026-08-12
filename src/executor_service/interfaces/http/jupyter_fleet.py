"""REST administration facade for the persistent Jupyter server fleet."""

from collections.abc import Awaitable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query

from executor_service.container import ApplicationContainer
from executor_service.domain.enums import JupyterPool, JupyterServerStatus
from executor_service.interfaces.http.schemas import (
    ErrorResponse,
    JupyterPoolPageResponse,
    JupyterPoolResponse,
    JupyterServerMutationRequest,
    JupyterServerPageResponse,
    JupyterServerProbeRequest,
    JupyterServerPurgeRequest,
    JupyterServerPurgeResponse,
    JupyterServerResponse,
    JupyterServerUpsertRequest,
)
from executor_service.tracing import TracingManager

FleetLimit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=2048)]

FLEET_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Invalid fleet configuration"},
    404: {"model": ErrorResponse, "description": "Jupyter server not found"},
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


def build_jupyter_fleet_router(container: ApplicationContainer) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["jupyter-fleet"])
    registry = container.jupyter_registry
    tracing = container.tracing

    @router.post(
        "/jupyter-servers",
        response_model=JupyterServerResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Register or update and immediately probe a Jupyter server",
    )
    async def upsert_jupyter_server(
        request: JupyterServerUpsertRequest,
    ) -> JupyterServerResponse:
        view = await _trace_call(
            tracing,
            "executor.http.jupyter_server_upsert",
            registry.upsert(request.to_command()),
            {"executor.jupyter.server.name": request.name},
        )
        return JupyterServerResponse.from_view(view)

    @router.get(
        "/jupyter-servers",
        response_model=JupyterServerPageResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="List Jupyter servers and current capacity",
    )
    async def list_jupyter_servers(
        pool: JupyterPool | None = None,
        server_status: Annotated[
            JupyterServerStatus | None, Query(alias="status")
        ] = None,
        enabled: bool | None = None,
        cursor: Cursor = None,
        limit: FleetLimit = 100,
    ) -> JupyterServerPageResponse:
        page = await registry.list(
            pool,
            status=server_status,
            enabled=enabled,
            cursor=cursor,
            limit=limit,
        )
        return JupyterServerPageResponse.from_page(page)

    @router.get(
        "/jupyter-pools",
        response_model=JupyterPoolPageResponse,
        summary="Get capacity and health summaries for all Jupyter pools",
    )
    async def list_jupyter_pools() -> JupyterPoolPageResponse:
        views = await registry.pool_summaries()
        return JupyterPoolPageResponse(
            items=[JupyterPoolResponse.from_view(view) for view in views]
        )

    @router.get(
        "/jupyter-servers/{server_id}",
        response_model=JupyterServerResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Get one Jupyter server without exposing its credential",
    )
    async def get_jupyter_server(server_id: UUID) -> JupyterServerResponse:
        view = await _trace_call(
            tracing,
            "executor.http.jupyter_server_get",
            registry.get(server_id),
            {"executor.jupyter.server.id": str(server_id)},
        )
        return JupyterServerResponse.from_view(view)

    @router.post(
        "/jupyter-servers/{server_id}/probe",
        response_model=JupyterServerResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Probe one enabled Jupyter server now",
    )
    async def probe_jupyter_server(
        server_id: UUID, request: JupyterServerProbeRequest
    ) -> JupyterServerResponse:
        view = await _trace_call(
            tracing,
            "executor.http.jupyter_server_probe",
            registry.probe(
                server_id,
                actor_type=request.actor.type,
                actor_id=request.actor.id,
            ),
            {"executor.jupyter.server.id": str(server_id)},
        )
        return JupyterServerResponse.from_view(view)

    @router.post(
        "/jupyter-servers/{server_id}/drain",
        response_model=JupyterServerResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Stop assigning new work while current work finishes",
    )
    async def drain_jupyter_server(
        server_id: UUID, request: JupyterServerMutationRequest
    ) -> JupyterServerResponse:
        view = await _trace_call(
            tracing,
            "executor.http.jupyter_server_drain",
            registry.set_state(
                request.to_state_command(server_id, JupyterServerStatus.DRAINING)
            ),
            {"executor.jupyter.server.id": str(server_id)},
        )
        return JupyterServerResponse.from_view(view)

    @router.post(
        "/jupyter-servers/{server_id}/activate",
        response_model=JupyterServerResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Enable, probe, and return a healthy server to active scheduling",
    )
    async def activate_jupyter_server(
        server_id: UUID, request: JupyterServerMutationRequest
    ) -> JupyterServerResponse:
        view = await _trace_call(
            tracing,
            "executor.http.jupyter_server_activate",
            registry.set_state(
                request.to_state_command(server_id, JupyterServerStatus.ACTIVE)
            ),
            {"executor.jupyter.server.id": str(server_id)},
        )
        return JupyterServerResponse.from_view(view)

    @router.delete(
        "/jupyter-servers/{server_id}",
        response_model=JupyterServerResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Soft-delete a server while preserving execution history",
    )
    async def remove_jupyter_server(
        server_id: UUID, request: JupyterServerMutationRequest
    ) -> JupyterServerResponse:
        view = await _trace_call(
            tracing,
            "executor.http.jupyter_server_remove",
            registry.remove(request.to_remove_command(server_id)),
            {"executor.jupyter.server.id": str(server_id)},
        )
        return JupyterServerResponse.from_view(view)

    @router.post(
        "/jupyter-servers/{server_id}/purge",
        response_model=JupyterServerPurgeResponse,
        responses=FLEET_ERROR_RESPONSES,
        summary="Permanently remove an unused, already disabled server",
    )
    async def purge_jupyter_server(
        server_id: UUID, request: JupyterServerPurgeRequest
    ) -> JupyterServerPurgeResponse:
        view = await _trace_call(
            tracing,
            "executor.http.jupyter_server_purge",
            registry.purge(request.to_command(server_id)),
            {"executor.jupyter.server.id": str(server_id)},
        )
        return JupyterServerPurgeResponse.from_view(view)

    return router
