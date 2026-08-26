"""REST administration facade for Executor-wide maintenance."""

from typing import Any

from fastapi import APIRouter

from executor_service.application.maintenance import (
    SetExecutorAdmissionCommand,
)
from executor_service.container import ApplicationContainer
from executor_service.domain.enums import ExecutorAdmissionState
from executor_service.interfaces.contracts import ExecutorMaintenanceResponse
from executor_service.interfaces.http.schemas import (
    ErrorResponse,
    ExecutorMaintenanceMutationRequest,
)

MAINTENANCE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {
        "model": ErrorResponse,
        "description": "Idempotency conflict",
    },
    422: {"model": ErrorResponse, "description": "Invalid request"},
}


def build_maintenance_router(
    container: ApplicationContainer,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["maintenance"])
    maintenance = container.maintenance
    tracing = container.tracing

    @router.get(
        "/maintenance",
        response_model=ExecutorMaintenanceResponse,
        summary="Get Executor-wide admission and shutdown readiness",
    )
    async def get_maintenance() -> ExecutorMaintenanceResponse:
        with tracing.span("executor.http.maintenance_get"):
            view = await maintenance.get()
        return ExecutorMaintenanceResponse.from_view(view)

    async def set_state(
        request: ExecutorMaintenanceMutationRequest,
        desired_state: ExecutorAdmissionState,
    ) -> ExecutorMaintenanceResponse:
        with tracing.span(
            "executor.http.maintenance_set_state",
            attributes={
                "executor.maintenance.admission_state": desired_state.value
            },
        ):
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

    return router
