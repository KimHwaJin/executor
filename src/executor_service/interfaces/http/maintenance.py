"""REST administration facade for Executor-wide maintenance."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from executor_service.application.maintenance import (
    SetExecutorAdmissionCommand,
)
from executor_service.container import ApplicationContainer
from executor_service.domain.enums import ExecutorAdmissionState
from executor_service.interfaces.contracts import (
    ExecutorMaintenanceResponse,
    MaintenanceRunResponse,
    MaintenanceRunTargetPageResponse,
)
from executor_service.interfaces.http.schemas import (
    ErrorResponse,
    ExecutorMaintenanceMutationRequest,
    MaintenanceRunCreateRequest,
)

RunTargetLimit = Annotated[int, Query(ge=1, le=200)]
Cursor = Annotated[str | None, Query(max_length=2048)]

MAINTENANCE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ErrorResponse,
        "description": "Idempotency conflict",
    },
    422: {"model": ErrorResponse, "description": "Invalid request"},
}
MAINTENANCE_RUN_READ_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **MAINTENANCE_ERROR_RESPONSES,
    404: {"model": ErrorResponse, "description": "Maintenance Run not found"},
}


def build_maintenance_router(
    container: ApplicationContainer,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["maintenance"])
    maintenance = container.maintenance
    maintenance_runs = container.maintenance_runs

    @router.get(
        "/maintenance",
        response_model=ExecutorMaintenanceResponse,
        summary="Get Executor-wide admission and shutdown readiness",
    )
    async def get_maintenance() -> ExecutorMaintenanceResponse:
        view = await maintenance.get()
        return ExecutorMaintenanceResponse.from_view(view)

    async def set_state(
        request: ExecutorMaintenanceMutationRequest,
        desired_state: ExecutorAdmissionState,
    ) -> ExecutorMaintenanceResponse:
        view = await maintenance.set_state(
            SetExecutorAdmissionCommand(
                idempotency_key=request.idempotency_key,
                desired_state=desired_state,
                actor_type=request.actor.type,
                actor_id=request.actor.id,
            )
        )
        return ExecutorMaintenanceResponse.from_view(view)

    @router.post(
        "/maintenance/drain",
        response_model=ExecutorMaintenanceResponse,
        responses=MAINTENANCE_ERROR_RESPONSES,
        summary="Stop admission of new Executions across all Workers",
    )
    async def drain(
        request: ExecutorMaintenanceMutationRequest,
    ) -> ExecutorMaintenanceResponse:
        return await set_state(request, ExecutorAdmissionState.DRAINING)

    @router.post(
        "/maintenance/activate",
        response_model=ExecutorMaintenanceResponse,
        responses=MAINTENANCE_ERROR_RESPONSES,
        summary="Resume admission of queued Executions across all Workers",
    )
    async def activate(
        request: ExecutorMaintenanceMutationRequest,
    ) -> ExecutorMaintenanceResponse:
        return await set_state(request, ExecutorAdmissionState.ACTIVE)

    @router.post(
        "/maintenance/runs",
        response_model=MaintenanceRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=MAINTENANCE_ERROR_RESPONSES,
        summary="Create a durable Run that stops active Executions",
    )
    async def create_run(
        request: MaintenanceRunCreateRequest,
        response: Response,
    ) -> MaintenanceRunResponse:
        view = await maintenance_runs.create(request.to_command())
        response.headers["Location"] = f"/api/v1/maintenance/runs/{view.id}"
        return MaintenanceRunResponse.from_view(view)

    @router.get(
        "/maintenance/runs/{run_id}",
        response_model=MaintenanceRunResponse,
        responses=MAINTENANCE_RUN_READ_ERROR_RESPONSES,
        summary="Get one Maintenance Run and its target counts",
    )
    async def get_run(run_id: UUID) -> MaintenanceRunResponse:
        view = await maintenance_runs.get(run_id)
        return MaintenanceRunResponse.from_view(view)

    @router.get(
        "/maintenance/runs/{run_id}/targets",
        response_model=MaintenanceRunTargetPageResponse,
        responses=MAINTENANCE_RUN_READ_ERROR_RESPONSES,
        summary="List the Execution targets captured by a Maintenance Run",
    )
    async def list_run_targets(
        run_id: UUID,
        cursor: Cursor = None,
        limit: RunTargetLimit = 100,
    ) -> MaintenanceRunTargetPageResponse:
        page = await maintenance_runs.list_targets(
            run_id, cursor=cursor, limit=limit
        )
        return MaintenanceRunTargetPageResponse.from_page(page)

    return router
