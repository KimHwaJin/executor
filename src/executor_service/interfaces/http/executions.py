"""Versioned REST facade for Executor execution lifecycle and trace queries."""

from collections.abc import Awaitable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from executor_service.application.commands import (
    CancelExecutionCommand,
    ContinueExecutionCommand,
    FinishExecutionCommand,
    RetryExecutionCommand,
    StepSpec,
)
from executor_service.container import ApplicationContainer
from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.errors import ExecutionNotFoundError, InvalidExecutionSpecError
from executor_service.interfaces.http.schemas import (
    ErrorResponse,
    ExecutionArtifactResponse,
    ExecutionAttemptResponse,
    ExecutionCancelRequest,
    ExecutionContinueRequest,
    ExecutionEventResponse,
    ExecutionFinishRequest,
    ExecutionResponse,
    ExecutionRetryRequest,
    ExecutionStepResponse,
    ExecutionSubmitRequest,
    ExecutionTraceResponse,
    ExecutorCapabilitiesResponse,
)
from executor_service.tracing import TracingManager

ExecutionLimit = Annotated[int, Query(ge=1, le=200)]
AttemptLimit = Annotated[int, Query(ge=1, le=200)]
EventLimit = Annotated[int, Query(ge=1, le=500)]
ArtifactLimit = Annotated[int, Query(ge=1, le=1000)]

DOMAIN_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Execution or Artifact not found"},
    409: {"model": ErrorResponse, "description": "State, version, or idempotency conflict"},
    422: {"model": ErrorResponse, "description": "Invalid ExecutionSpec"},
}


async def _trace_call[T](
    tracing: TracingManager,
    name: str,
    operation: Awaitable[T],
    attributes: dict[str, object] | None = None,
) -> T:
    with tracing.span(name, attributes=attributes):
        return await operation


def build_execution_router(container: ApplicationContainer) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["executions"])
    execution_service = container.execution_service
    execution_queries = container.execution_queries
    resolver = container.execution_spec_resolver
    tracing = container.tracing

    @router.get(
        "/capabilities",
        response_model=ExecutorCapabilitiesResponse,
        summary="Get Executor capabilities",
    )
    async def capabilities() -> ExecutorCapabilitiesResponse:
        return ExecutorCapabilitiesResponse()

    @router.post(
        "/executions",
        response_model=ExecutionResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Submit an asynchronous execution",
    )
    async def submit_execution(
        request: ExecutionSubmitRequest, response: Response
    ) -> ExecutionResponse:
        resolved = await resolver.resolve(request.source)
        if resolved.spec.steps[0].sequence != 0:
            raise InvalidExecutionSpecError(
                "Execution submit requires an ExecutionSpec starting at sequence 0."
            )
        execution = await _trace_call(
            tracing,
            "executor.http.execution_submit",
            execution_service.submit(
                request.to_command(
                    resolved.spec,
                    source_content=resolved.canonical_content,
                    source_sha256=resolved.sha256,
                )
            ),
        )
        response.headers["Location"] = f"/api/v1/executions/{execution.id}"
        return ExecutionResponse.from_domain(execution)

    @router.get(
        "/executions",
        response_model=list[ExecutionResponse],
        summary="List execution history",
    )
    async def list_executions(
        requested_by_user_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        execution_status: Annotated[ExecutionStatus | None, Query(alias="status")] = None,
        limit: ExecutionLimit = 100,
    ) -> list[ExecutionResponse]:
        executions = await execution_queries.executions(
            requested_by_user_id=requested_by_user_id,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            status=execution_status,
            limit=limit,
        )
        return [ExecutionResponse.from_domain(execution) for execution in executions]

    @router.get(
        "/executions/{execution_id}",
        response_model=ExecutionResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Get current execution state",
    )
    async def get_execution(execution_id: UUID) -> ExecutionResponse:
        execution = await _trace_call(
            tracing,
            "executor.http.execution_get",
            execution_service.get(execution_id),
            {"executor.execution.id": str(execution_id)},
        )
        return ExecutionResponse.from_domain(execution)

    @router.post(
        "/executions/{execution_id}/cancel",
        response_model=ExecutionResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Request asynchronous cancellation",
    )
    async def cancel_execution(
        execution_id: UUID, request: ExecutionCancelRequest
    ) -> ExecutionResponse:
        execution = await _trace_call(
            tracing,
            "executor.http.execution_cancel",
            execution_service.cancel(
                CancelExecutionCommand(
                    execution_id=execution_id,
                    idempotency_key=request.idempotency_key,
                    reason=request.reason,
                )
            ),
            {"executor.execution.id": str(execution_id)},
        )
        return ExecutionResponse.from_domain(execution)

    @router.post(
        "/executions/{execution_id}/retry",
        response_model=ExecutionResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Retry a failed execution",
    )
    async def retry_execution(
        execution_id: UUID, request: ExecutionRetryRequest
    ) -> ExecutionResponse:
        execution = await _trace_call(
            tracing,
            "executor.http.execution_retry",
            execution_service.retry(
                RetryExecutionCommand(
                    execution_id=execution_id,
                    idempotency_key=request.idempotency_key,
                )
            ),
            {"executor.execution.id": str(execution_id)},
        )
        return ExecutionResponse.from_domain(execution)

    @router.post(
        "/executions/{execution_id}/continue",
        response_model=ExecutionResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Append the next dynamic execution Step",
    )
    async def continue_execution(
        execution_id: UUID, request: ExecutionContinueRequest
    ) -> ExecutionResponse:
        resolved = await resolver.resolve(request.source)
        if len(resolved.spec.steps) != 1:
            raise InvalidExecutionSpecError(
                "DYNAMIC continue requires exactly one ExecutionSpec step."
            )
        source_step = resolved.spec.steps[0]
        execution = await _trace_call(
            tracing,
            "executor.http.execution_continue",
            execution_service.continue_execution(
                ContinueExecutionCommand(
                    execution_id=execution_id,
                    idempotency_key=request.idempotency_key,
                    expected_version=request.expected_version,
                    step=StepSpec(
                        sequence=source_step.sequence,
                        code=source_step.code,
                        execution_plan_id=resolved.spec.execution_plan_id,
                        plan_step_id=source_step.plan_step_id,
                        skill_name=source_step.skill_name,
                        tool_name=source_step.tool_name,
                        input_parameters=source_step.input_parameters,
                    ),
                )
            ),
            {
                "executor.execution.id": str(execution_id),
                "executor.step.sequence": source_step.sequence,
            },
        )
        return ExecutionResponse.from_domain(execution)

    @router.post(
        "/executions/{execution_id}/finish",
        response_model=ExecutionResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Finalize a waiting dynamic execution",
    )
    async def finish_execution(
        execution_id: UUID, request: ExecutionFinishRequest
    ) -> ExecutionResponse:
        execution = await _trace_call(
            tracing,
            "executor.http.execution_finish",
            execution_service.finish_execution(
                FinishExecutionCommand(
                    execution_id=execution_id,
                    idempotency_key=request.idempotency_key,
                    expected_version=request.expected_version,
                )
            ),
            {"executor.execution.id": str(execution_id)},
        )
        return ExecutionResponse.from_domain(execution)

    @router.get(
        "/executions/{execution_id}/steps",
        response_model=list[ExecutionStepResponse],
        responses=DOMAIN_ERROR_RESPONSES,
        summary="List current execution Steps",
    )
    async def list_execution_steps(execution_id: UUID) -> list[ExecutionStepResponse]:
        execution = await execution_service.get(execution_id)
        return [ExecutionStepResponse.from_domain(step) for step in execution.steps]

    @router.get(
        "/executions/{execution_id}/steps/{step_id}",
        response_model=ExecutionStepResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Get one current execution Step",
    )
    async def get_execution_step(execution_id: UUID, step_id: UUID) -> ExecutionStepResponse:
        execution = await execution_service.get(execution_id)
        step = next((item for item in execution.steps if item.id == step_id), None)
        if step is None:
            raise ExecutionNotFoundError(
                f"Execution Step {step_id} was not found in Execution {execution_id}."
            )
        return ExecutionStepResponse.from_domain(step)

    @router.get(
        "/executions/{execution_id}/attempts",
        response_model=list[ExecutionAttemptResponse],
        responses=DOMAIN_ERROR_RESPONSES,
        summary="List immutable execution Attempts and Step Attempts",
    )
    async def list_execution_attempts(
        execution_id: UUID, limit: AttemptLimit = 100
    ) -> list[ExecutionAttemptResponse]:
        views = await execution_queries.attempts(execution_id, limit=limit)
        return [ExecutionAttemptResponse.from_view(view) for view in views]

    @router.get(
        "/executions/{execution_id}/events",
        response_model=list[ExecutionEventResponse],
        responses=DOMAIN_ERROR_RESPONSES,
        summary="List the transactional Outbox event timeline",
    )
    async def list_execution_events(
        execution_id: UUID, limit: EventLimit = 200
    ) -> list[ExecutionEventResponse]:
        views = await execution_queries.events(execution_id, limit=limit)
        return [ExecutionEventResponse.from_view(view) for view in views]

    @router.get(
        "/executions/{execution_id}/artifacts",
        response_model=list[ExecutionArtifactResponse],
        responses=DOMAIN_ERROR_RESPONSES,
        summary="List execution Artifacts",
    )
    async def list_execution_artifacts(
        execution_id: UUID, limit: ArtifactLimit = 500
    ) -> list[ExecutionArtifactResponse]:
        views = await execution_queries.artifacts(execution_id, limit=limit)
        return [ExecutionArtifactResponse.from_view(view) for view in views]

    @router.get(
        "/executions/{execution_id}/trace",
        response_model=ExecutionTraceResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Get the complete execution trace",
    )
    async def get_execution_trace(execution_id: UUID) -> ExecutionTraceResponse:
        view = await execution_queries.trace(execution_id)
        return ExecutionTraceResponse.from_view(view)

    @router.get(
        "/artifacts/{artifact_id}",
        response_model=ExecutionArtifactResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Get one execution Artifact and lineage references",
    )
    async def get_execution_artifact(artifact_id: UUID) -> ExecutionArtifactResponse:
        view = await execution_queries.artifact(artifact_id)
        return ExecutionArtifactResponse.from_view(view)

    return router
