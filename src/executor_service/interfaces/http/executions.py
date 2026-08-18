"""Versioned REST facade for Executor execution lifecycle and history queries."""

from collections.abc import Awaitable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query, Response, status

from executor_service.application.commands import (
    CancelExecutionCommand,
    CreateOperationCommand,
    FinalizeExecutionCommand,
    RetryExecutionCommand,
    StepSpec,
)
from executor_service.application.notebook_queries import NotebookResponseFormat
from executor_service.container import ApplicationContainer
from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.errors import ExecutionNotFoundError, InvalidExecutionSpecError
from executor_service.execution_specs import PathCodeSource
from executor_service.interfaces.contracts import (
    ExecutionArtifactPageResponse,
    ExecutionArtifactResponse,
    ExecutionAttemptDetailResponse,
    ExecutionAttemptPageResponse,
    ExecutionCommandResponse,
    ExecutionEventPageResponse,
    ExecutionNotebookCellResponse,
    ExecutionNotebookResponse,
    ExecutionOperationPageResponse,
    ExecutionOperationResponse,
    ExecutionPageResponse,
    ExecutionResponse,
    ExecutionStepAttemptPageResponse,
    ExecutionStepPageResponse,
    ExecutionStepResponse,
    ExecutionSubmitRequest,
)
from executor_service.interfaces.http.schemas import (
    ErrorResponse,
    ExecutionCancelRequest,
    ExecutionFinalizeRequest,
    ExecutionOperationCreateRequest,
    ExecutionRetryRequest,
)
from executor_service.tracing import TracingManager

ExecutionLimit = Annotated[int, Query(ge=1, le=200)]
AttemptLimit = Annotated[int, Query(ge=1, le=200)]
EventLimit = Annotated[int, Query(ge=1, le=500)]
ArtifactLimit = Annotated[int, Query(ge=1, le=1000)]
Cursor = Annotated[str | None, Query(max_length=2048)]
NotebookLimit = Annotated[int, Query(ge=0, le=200)]
NotebookStartIndex = Annotated[int, Query(ge=0)]

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

    @router.post(
        "/executions",
        response_model=ExecutionCommandResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Submit an asynchronous execution",
    )
    async def submit_execution(
        request: ExecutionSubmitRequest, response: Response
    ) -> ExecutionCommandResponse:
        resolved = await resolver.resolve(request.operation.source)
        if resolved.spec.steps[0].sequence != 0:
            raise InvalidExecutionSpecError(
                "Execution submit requires an ExecutionSpec starting at sequence 0."
            )
        result = await _trace_call(
            tracing,
            "executor.http.execution_submit",
            execution_service.submit_result(
                request.to_command(
                    resolved.spec,
                    source_content=resolved.canonical_content,
                    source_sha256=resolved.sha256,
                )
            ),
        )
        execution = result.execution
        response.headers["Location"] = f"/api/v1/executions/{execution.id}"
        return ExecutionCommandResponse.from_domain(execution, operation_id=result.operation_id)

    @router.get(
        "/executions",
        response_model=ExecutionPageResponse,
        summary="List execution history",
    )
    async def list_executions(
        user_id: str | None = None,
        project_id: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        execution_status: Annotated[ExecutionStatus | None, Query(alias="status")] = None,
        cursor: Cursor = None,
        limit: ExecutionLimit = 100,
    ) -> ExecutionPageResponse:
        page = await execution_queries.executions(
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            status=execution_status,
            cursor=cursor,
            limit=limit,
        )
        return ExecutionPageResponse.from_page(page)

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
            execution_queries.execution(execution_id),
            {"executor.execution.id": str(execution_id)},
        )
        return ExecutionResponse.from_view(execution)

    @router.get(
        "/executions/{execution_id}/notebook",
        response_model=ExecutionNotebookResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Read Runtime-owned execution notebook cells",
    )
    async def read_execution_notebook(
        execution_id: UUID,
        response_format: NotebookResponseFormat = "brief",
        start_index: NotebookStartIndex = 0,
        limit: NotebookLimit = 20,
    ) -> ExecutionNotebookResponse:
        view = await container.notebook_queries.read_notebook(
            execution_id,
            response_format=response_format,
            start_index=start_index,
            limit=limit,
        )
        return ExecutionNotebookResponse.from_view(view)

    @router.get(
        "/executions/{execution_id}/notebook/cells/{cell_index}",
        response_model=ExecutionNotebookCellResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Read one Runtime-owned execution notebook cell",
    )
    async def read_execution_notebook_cell(
        execution_id: UUID,
        cell_index: Annotated[int, Path(ge=0)],
        include_outputs: bool = True,
    ) -> ExecutionNotebookCellResponse:
        view = await container.notebook_queries.read_cell(
            execution_id, cell_index, include_outputs=include_outputs
        )
        return ExecutionNotebookCellResponse.from_view(execution_id, view)

    @router.post(
        "/executions/{execution_id}/cancel",
        response_model=ExecutionCommandResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Request asynchronous cancellation",
    )
    async def cancel_execution(
        execution_id: UUID, request: ExecutionCancelRequest, response: Response
    ) -> ExecutionCommandResponse:
        execution = await _trace_call(
            tracing,
            "executor.http.execution_cancel",
            execution_service.cancel(
                CancelExecutionCommand(
                    execution_id=execution_id,
                    idempotency_key=request.idempotency_key,
                    reason=request.reason,
                    actor_type=request.actor.type,
                    actor_id=request.actor.id,
                )
            ),
            {"executor.execution.id": str(execution_id)},
        )
        response.headers["Location"] = f"/api/v1/executions/{execution.id}"
        return ExecutionCommandResponse.from_domain(execution)

    @router.post(
        "/executions/{execution_id}/retry",
        response_model=ExecutionCommandResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Retry a failed execution",
    )
    async def retry_execution(
        execution_id: UUID, request: ExecutionRetryRequest, response: Response
    ) -> ExecutionCommandResponse:
        result = await _trace_call(
            tracing,
            "executor.http.execution_retry",
            execution_service.retry_result(
                RetryExecutionCommand(
                    execution_id=execution_id,
                    idempotency_key=request.idempotency_key,
                    actor_type=request.actor.type,
                    actor_id=request.actor.id,
                )
            ),
            {"executor.execution.id": str(execution_id)},
        )
        response.headers["Location"] = f"/api/v1/executions/{result.execution.id}"
        return ExecutionCommandResponse.from_domain(
            result.execution, operation_id=result.operation_id
        )

    @router.post(
        "/executions/{execution_id}/operations",
        response_model=ExecutionCommandResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Append the next Operation to a MULTI execution",
    )
    async def create_operation(
        execution_id: UUID, request: ExecutionOperationCreateRequest, response: Response
    ) -> ExecutionCommandResponse:
        resolved = await resolver.resolve(request.source)
        source_steps = resolved.spec.steps
        result = await _trace_call(
            tracing,
            "executor.http.execution_operation_create",
            execution_service.create_operation_result(
                CreateOperationCommand(
                    execution_id=execution_id,
                    idempotency_key=request.idempotency_key,
                    expected_version=request.expected_version,
                    source_content=resolved.canonical_content,
                    operation_timeout_seconds=request.operation_timeout_seconds,
                    metadata=request.metadata,
                    code_source_type=request.source.type,
                    code_path=(
                        request.source.path if isinstance(request.source, PathCodeSource) else None
                    ),
                    source_sha256=resolved.sha256,
                    steps=tuple(
                        StepSpec(
                            sequence=source_step.sequence,
                            code=source_step.code,
                            step_timeout_seconds=source_step.step_timeout_seconds,
                            skill_name=source_step.skill_name,
                            tool_name=source_step.tool_name,
                            input_parameters=source_step.input_parameters,
                        )
                        for source_step in source_steps
                    ),
                    actor_type=request.actor.type,
                    actor_id=request.actor.id,
                )
            ),
            {
                "executor.execution.id": str(execution_id),
                "executor.operation.first_sequence": source_steps[0].sequence,
                "executor.operation.last_sequence": source_steps[-1].sequence,
            },
        )
        execution = result.execution
        response.headers["Location"] = f"/api/v1/executions/{execution.id}"
        return ExecutionCommandResponse.from_domain(execution, operation_id=result.operation_id)

    @router.post(
        "/executions/{execution_id}/finalize",
        response_model=ExecutionCommandResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Finalize a waiting MULTI execution",
    )
    async def finalize_execution(
        execution_id: UUID, request: ExecutionFinalizeRequest, response: Response
    ) -> ExecutionCommandResponse:
        execution = await _trace_call(
            tracing,
            "executor.http.execution_finalize",
            execution_service.finalize_execution(
                FinalizeExecutionCommand(
                    execution_id=execution_id,
                    idempotency_key=request.idempotency_key,
                    expected_version=request.expected_version,
                    actor_type=request.actor.type,
                    actor_id=request.actor.id,
                )
            ),
            {"executor.execution.id": str(execution_id)},
        )
        response.headers["Location"] = f"/api/v1/executions/{execution.id}"
        return ExecutionCommandResponse.from_domain(execution)

    @router.get(
        "/executions/{execution_id}/steps",
        response_model=ExecutionStepPageResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="List current execution Steps",
    )
    async def list_execution_steps(
        execution_id: UUID,
        cursor: Cursor = None,
        limit: ExecutionLimit = 100,
    ) -> ExecutionStepPageResponse:
        page = await execution_queries.steps(execution_id, cursor=cursor, limit=limit)
        return ExecutionStepPageResponse.from_page(page, execution_id)

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
        return ExecutionStepResponse.from_domain(step, execution_id)

    @router.get(
        "/executions/{execution_id}/operations",
        response_model=ExecutionOperationPageResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="List Agent-submitted execution Operations",
    )
    async def list_execution_operations(
        execution_id: UUID,
        cursor: Cursor = None,
        limit: ExecutionLimit = 100,
    ) -> ExecutionOperationPageResponse:
        page = await execution_queries.operations(execution_id, cursor=cursor, limit=limit)
        return ExecutionOperationPageResponse.from_page(page)

    @router.get(
        "/executions/{execution_id}/operations/{operation_id}",
        response_model=ExecutionOperationResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Get one execution Operation result",
    )
    async def get_execution_operation(
        execution_id: UUID, operation_id: UUID
    ) -> ExecutionOperationResponse:
        view = await execution_queries.operation(execution_id, operation_id)
        return ExecutionOperationResponse.from_view(view)

    @router.get(
        "/executions/{execution_id}/operations/{operation_id}/steps",
        response_model=ExecutionStepPageResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="List current Step results for one Operation",
    )
    async def list_execution_operation_steps(
        execution_id: UUID,
        operation_id: UUID,
        cursor: Cursor = None,
        limit: ExecutionLimit = 100,
    ) -> ExecutionStepPageResponse:
        page = await execution_queries.operation_steps(
            execution_id, operation_id, cursor=cursor, limit=limit
        )
        return ExecutionStepPageResponse.from_page(page, execution_id)

    @router.get(
        "/executions/{execution_id}/attempts",
        response_model=ExecutionAttemptPageResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="List immutable execution Attempts and Step Attempts",
    )
    async def list_execution_attempts(
        execution_id: UUID,
        cursor: Cursor = None,
        limit: AttemptLimit = 100,
    ) -> ExecutionAttemptPageResponse:
        page = await execution_queries.attempts(execution_id, cursor=cursor, limit=limit)
        return ExecutionAttemptPageResponse.from_page(page)

    @router.get(
        "/executions/{execution_id}/attempts/{attempt_id}",
        response_model=ExecutionAttemptDetailResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Get one immutable execution Attempt",
    )
    async def get_execution_attempt(
        execution_id: UUID, attempt_id: UUID
    ) -> ExecutionAttemptDetailResponse:
        view = await execution_queries.attempt(execution_id, attempt_id)
        return ExecutionAttemptDetailResponse.from_view(view)

    @router.get(
        "/executions/{execution_id}/attempts/{attempt_id}/steps",
        response_model=ExecutionStepAttemptPageResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="List immutable Step results for one Attempt",
    )
    async def list_execution_attempt_steps(
        execution_id: UUID,
        attempt_id: UUID,
        cursor: Cursor = None,
        limit: ExecutionLimit = 100,
    ) -> ExecutionStepAttemptPageResponse:
        page = await execution_queries.attempt_steps(
            execution_id,
            attempt_id,
            cursor=cursor,
            limit=limit,
        )
        return ExecutionStepAttemptPageResponse.from_page(page)

    @router.get(
        "/executions/{execution_id}/events",
        response_model=ExecutionEventPageResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="List the transactional Outbox event timeline",
    )
    async def list_execution_events(
        execution_id: UUID,
        cursor: Cursor = None,
        limit: EventLimit = 200,
    ) -> ExecutionEventPageResponse:
        page = await execution_queries.events(execution_id, cursor=cursor, limit=limit)
        return ExecutionEventPageResponse.from_page(page)

    @router.get(
        "/executions/{execution_id}/artifacts",
        response_model=ExecutionArtifactPageResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="List execution Artifacts",
    )
    async def list_execution_artifacts(
        execution_id: UUID,
        cursor: Cursor = None,
        limit: ArtifactLimit = 500,
    ) -> ExecutionArtifactPageResponse:
        page = await execution_queries.artifacts(execution_id, cursor=cursor, limit=limit)
        return ExecutionArtifactPageResponse.from_page(page)

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
