"""Versioned REST facade for Executor execution lifecycle and history queries."""

from collections.abc import Awaitable
from typing import Annotated, Any
from urllib.parse import quote
from uuid import UUID

from fastapi import (
    APIRouter,
    Header,
    Path,
    Query,
    Response,
    status,
)
from fastapi.responses import StreamingResponse

from executor_service.application.commands import (
    CancelExecutionCommand,
    CreateOperationCommand,
    FinalizeExecutionCommand,
    RetryExecutionCommand,
    StepSpec,
)
from executor_service.application.notebook_queries import (
    NotebookResponseFormat,
)
from executor_service.container import ApplicationContainer
from executor_service.domain.enums import ExecutionStatus
from executor_service.domain.errors import (
    InvalidExecutionSpecError,
)
from executor_service.interfaces.contracts import (
    ExecutionArtifactMaterializeRequest,
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
    ExecutionOperationResultResponse,
    ExecutionPageResponse,
    ExecutionResponse,
    ExecutionResultResponse,
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
EventSequence = Annotated[int, Query(ge=0)]
ArtifactLimit = Annotated[int, Query(ge=1, le=1000)]
Cursor = Annotated[str | None, Query(max_length=2048)]
NotebookLimit = Annotated[int, Query(ge=1, le=200)]
NotebookStartIndex = Annotated[int, Query(ge=0)]

DOMAIN_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {
        "model": ErrorResponse,
        "description": "Execution or Artifact not found",
    },
    409: {
        "model": ErrorResponse,
        "description": "State, version, or idempotency conflict",
    },
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
    execution_results = container.execution_results
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
        resolved = await resolver.resolve(request.operation.spec)
        if resolved.spec.steps[0].sequence != 0:
            raise InvalidExecutionSpecError(
                "Execution submit requires an ExecutionSpec starting at sequence 0."
            )
        result = await _trace_call(
            tracing,
            "executor.http.execution_submit",
            execution_service.submit_result(request.to_command(resolved)),
        )
        execution = result.execution
        response.headers["Location"] = f"/api/v1/executions/{execution.id}"
        return ExecutionCommandResponse.from_domain(
            execution, operation_id=result.operation_id
        )

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
        workflow_id: str | None = None,
        execution_status: Annotated[
            ExecutionStatus | None, Query(alias="status")
        ] = None,
        cursor: Cursor = None,
        limit: ExecutionLimit = 100,
    ) -> ExecutionPageResponse:
        page = await execution_queries.executions(
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            workflow_id=workflow_id,
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
        "/executions/{execution_id}/result",
        response_model=ExecutionResultResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Get the consolidated execution result for Agent reporting",
    )
    async def get_execution_result(
        execution_id: UUID,
    ) -> ExecutionResultResponse:
        bundle = await _trace_call(
            tracing,
            "executor.http.execution_result_get",
            execution_results.execution(execution_id),
            {"executor.execution.id": str(execution_id)},
        )
        return ExecutionResultResponse.from_bundle(bundle)

    @router.get(
        "/executions/{execution_id}/notebook",
        response_model=ExecutionNotebookResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Read Runtime-owned execution notebook cells",
    )
    async def read_execution_notebook(
        execution_id: UUID,
        view: NotebookResponseFormat = "SUMMARY",
        start_index: NotebookStartIndex = 0,
        limit: NotebookLimit = 20,
    ) -> ExecutionNotebookResponse:
        notebook = await container.notebook_queries.read_notebook(
            execution_id,
            view=view,
            start_index=start_index,
            limit=limit,
        )
        return ExecutionNotebookResponse.from_view(notebook)

    @router.get(
        "/executions/{execution_id}/notebook/cells/{cell_index}",
        response_model=ExecutionNotebookCellResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Read one Runtime-owned execution notebook cell",
    )
    async def read_execution_notebook_cell(
        execution_id: UUID,
        cell_index: Annotated[int, Path(ge=0)],
    ) -> ExecutionNotebookCellResponse:
        view = await container.notebook_queries.read_cell(
            execution_id, cell_index
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
        response.headers["Location"] = (
            f"/api/v1/executions/{result.execution.id}"
        )
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
        execution_id: UUID,
        request: ExecutionOperationCreateRequest,
        response: Response,
    ) -> ExecutionCommandResponse:
        resolved = await resolver.resolve(request.spec)
        source_steps = resolved.steps
        result = await _trace_call(
            tracing,
            "executor.http.execution_operation_create",
            execution_service.create_operation_result(
                CreateOperationCommand(
                    execution_id=execution_id,
                    idempotency_key=request.idempotency_key,
                    expected_version=request.expected_version,
                    spec_schema_version=resolved.spec.schema_version,
                    operation_timeout_seconds=request.operation_timeout_seconds,
                    metadata=request.metadata,
                    steps=tuple(
                        StepSpec(
                            sequence=source_step.sequence,
                            code=source_step.content,
                            source_type=source_step.source_type,
                            source_path=source_step.source_path,
                            source_sha256=source_step.source_sha256,
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
        response.headers["Location"] = (
            f"/api/v1/executions/{execution.id}/operations/"
            f"{result.operation_id}"
        )
        return ExecutionCommandResponse.from_domain(
            execution, operation_id=result.operation_id
        )

    @router.post(
        "/executions/{execution_id}/finalize",
        response_model=ExecutionCommandResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Finalize a waiting MULTI execution",
    )
    async def finalize_execution(
        execution_id: UUID,
        request: ExecutionFinalizeRequest,
        response: Response,
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
        page = await execution_queries.steps(
            execution_id, cursor=cursor, limit=limit
        )
        return ExecutionStepPageResponse.from_page(page, execution_id)

    @router.get(
        "/executions/{execution_id}/steps/{step_id}",
        response_model=ExecutionStepResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Get one current execution Step",
    )
    async def get_execution_step(
        execution_id: UUID, step_id: UUID
    ) -> ExecutionStepResponse:
        step = await execution_queries.step(execution_id, step_id)
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
        page = await execution_queries.operations(
            execution_id, cursor=cursor, limit=limit
        )
        return ExecutionOperationPageResponse.from_page(page)

    @router.get(
        "/executions/{execution_id}/operations/{operation_id}",
        response_model=ExecutionOperationResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Get one execution Operation detail",
    )
    async def get_execution_operation(
        execution_id: UUID, operation_id: UUID
    ) -> ExecutionOperationResponse:
        view = await execution_queries.operation(execution_id, operation_id)
        return ExecutionOperationResponse.from_view(view)

    @router.get(
        "/executions/{execution_id}/operations/{operation_id}/result",
        response_model=ExecutionOperationResultResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Get one Operation and all of its Step results",
    )
    async def get_execution_operation_result(
        execution_id: UUID, operation_id: UUID
    ) -> ExecutionOperationResultResponse:
        bundle = await execution_results.operation(execution_id, operation_id)
        return ExecutionOperationResultResponse.from_bundle(bundle)

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
        summary="List immutable execution Attempt summaries",
    )
    async def list_execution_attempts(
        execution_id: UUID,
        cursor: Cursor = None,
        limit: AttemptLimit = 100,
    ) -> ExecutionAttemptPageResponse:
        page = await execution_queries.attempts(
            execution_id, cursor=cursor, limit=limit
        )
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
        summary="List the durable Execution event timeline",
    )
    async def list_execution_events(
        execution_id: UUID,
        after_sequence: EventSequence = 0,
        cursor: Cursor = None,
        limit: EventLimit = 200,
    ) -> ExecutionEventPageResponse:
        page = await execution_queries.events(
            execution_id,
            after_sequence=after_sequence,
            cursor=cursor,
            limit=limit,
        )
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
        limit: ArtifactLimit = 100,
    ) -> ExecutionArtifactPageResponse:
        page = await execution_queries.artifacts(
            execution_id, cursor=cursor, limit=limit
        )
        return ExecutionArtifactPageResponse.from_page(page)

    @router.post(
        "/executions/{execution_id}/artifacts",
        response_model=ExecutionArtifactResponse,
        status_code=status.HTTP_201_CREATED,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Materialize Agent-authored text as a Runtime-owned Artifact",
    )
    async def materialize_execution_artifact(
        execution_id: UUID,
        request: ExecutionArtifactMaterializeRequest,
        response: Response,
    ) -> ExecutionArtifactResponse:
        artifact_id = await container.materialized_artifacts.materialize(
            request.to_command(execution_id)
        )
        response.headers["Location"] = f"/api/v1/artifacts/{artifact_id}"
        view = await execution_queries.artifact(artifact_id)
        return ExecutionArtifactResponse.from_view(view)

    @router.get(
        "/artifacts/{artifact_id}",
        response_model=ExecutionArtifactResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Get one execution Artifact and lineage references",
    )
    async def get_execution_artifact(
        artifact_id: UUID,
    ) -> ExecutionArtifactResponse:
        view = await execution_queries.artifact(artifact_id)
        return ExecutionArtifactResponse.from_view(view)

    @router.get(
        "/artifacts/{artifact_id}/content",
        responses={
            **DOMAIN_ERROR_RESPONSES,
            206: {"description": "Requested Artifact byte range"},
            416: {"description": "Artifact byte range is not satisfiable"},
        },
        summary="Stream registered Artifact content",
    )
    async def download_execution_artifact(
        artifact_id: UUID,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
    ) -> StreamingResponse:
        content = await container.artifact_content.open(
            artifact_id, range_header
        )
        byte_range = content.byte_range
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(byte_range.length),
            "Content-Disposition": (
                "attachment; filename*=UTF-8''" + quote(content.name, safe="")
            ),
            "ETag": f'"{content.checksum_sha256}"',
            "X-Checksum-SHA256": content.checksum_sha256,
        }
        if byte_range.partial:
            headers["Content-Range"] = (
                f"bytes {byte_range.start}-{byte_range.end}/{byte_range.size}"
            )
        return StreamingResponse(
            content.body,
            status_code=(
                status.HTTP_206_PARTIAL_CONTENT
                if byte_range.partial
                else status.HTTP_200_OK
            ),
            media_type=content.media_type,
            headers=headers,
        )

    return router
