"""Official MCP Python SDK v2 server and tool registration."""

import json
import logging
from collections.abc import Awaitable
from typing import Annotated
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent, TextContent
from pydantic import Field

from executor_service.application.commands import (
    CancelExecutionCommand,
    CreateOperationCommand,
    FinalizeExecutionCommand,
    RetryExecutionCommand,
    StepSpec,
)
from executor_service.application.execution_queries import (
    ExecutionQueryService,
)
from executor_service.application.execution_results import (
    ExecutionResultQueryService,
)
from executor_service.application.notebook_queries import (
    ExecutionNotebookQueryService,
    NotebookCellView,
    NotebookResponseFormat,
)
from executor_service.application.runtime_targets import (
    DisableRuntimeTargetCommand,
    RuntimeTargetManager,
    SetRuntimeTargetStateCommand,
    UpsertRuntimeTargetCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    ExecutionStatus,
    RuntimePool,
    RuntimeTargetStatus,
    RuntimeType,
)
from executor_service.domain.errors import DomainError, ErrorCode
from executor_service.execution_specs import ExecutionSpecResolver
from executor_service.infrastructure.materialized_artifacts import (
    MaterializedArtifactService,
)
from executor_service.interfaces.contracts import (
    ExecutionArtifactPageResponse,
    ExecutionArtifactResponse,
    ExecutionAttemptDetailResponse,
    ExecutionAttemptPageResponse,
    ExecutionCommandResponse,
    ExecutionEventPageResponse,
    ExecutionNotebookResponse,
    ExecutionOperationPageResponse,
    ExecutionOperationResponse,
    ExecutionOperationResultResponse,
    ExecutionPageResponse,
    ExecutionResponse,
    ExecutionResultResponse,
    ExecutionStepAttemptPageResponse,
    ExecutionStepPageResponse,
    ExecutionSubmitRequest,
    RuntimeTargetPageResponse,
    RuntimeTargetResponse,
    RuntimeTargetUpsertRequest,
)
from executor_service.interfaces.mcp.schemas import (
    ExecutionArtifactMaterializeToolRequest,
    ExecutionCancelRequest,
    ExecutionFinalizeRequest,
    ExecutionOperationCreateRequest,
    ExecutionRetryRequest,
    RuntimeTargetDisableRequest,
    RuntimeTargetProbeRequest,
    RuntimeTargetSetStateRequest,
)
from executor_service.tracing import TracingManager

StandardLimit = Annotated[int, Field(ge=1, le=200)]
EventLimit = Annotated[int, Field(ge=1, le=500)]
ArtifactLimit = Annotated[int, Field(ge=1, le=1000)]


def _public_tool_error(exc: Exception) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, DomainError):
        return ToolError(f"[{exc.code}] {exc}")
    logging.getLogger(__name__).exception(
        "Unhandled MCP Tool error", exc_info=exc
    )
    return ToolError(
        f"[{ErrorCode.INTERNAL_ERROR}] An internal error occurred."
    )


async def _trace_call[T](
    tracing: TracingManager | None,
    name: str,
    operation: Awaitable[T],
    attributes: dict[str, object] | None = None,
) -> T:
    if tracing is None:
        return await operation
    with tracing.span(name, attributes=attributes):
        return await operation


def build_mcp_server(
    execution_service: ExecutionService,
    runtime_manager: RuntimeTargetManager | None = None,
    execution_queries: ExecutionQueryService | None = None,
    tracing: TracingManager | None = None,
    execution_spec_resolver: ExecutionSpecResolver | None = None,
    notebook_queries: ExecutionNotebookQueryService | None = None,
    execution_results: ExecutionResultQueryService | None = None,
    materialized_artifacts: MaterializedArtifactService | None = None,
) -> MCPServer:
    server = MCPServer(
        name="executor-service",
        version="0.1.0",
        instructions=(
            "Submit asynchronous code executions and use execution_id to query or cancel them. "
            "Tool completion does not mean the submitted execution has finished."
        ),
    )

    @server.tool(
        description=(
            "Persist an asynchronous execution request and return immediately with execution_id. "
            "Reusing idempotency_key with the same request returns the original execution."
        )
    )
    async def execution_submit(
        request: ExecutionSubmitRequest,
    ) -> ExecutionCommandResponse:
        try:
            if execution_spec_resolver is None:
                raise ToolError(
                    f"[{ErrorCode.INTERNAL_ERROR}] ExecutionSpec resolver is not configured."
                )
            resolved = await execution_spec_resolver.resolve(
                request.operation.spec
            )
            if resolved.spec.steps[0].sequence != 0:
                raise ToolError(
                    f"[{ErrorCode.INVALID_EXECUTION_SPEC}] Execution submit requires an "
                    "ExecutionSpec starting at sequence 0."
                )
            result = await _trace_call(
                tracing,
                "executor.mcp.execution_submit",
                execution_service.submit_result(request.to_command(resolved)),
            )
        except Exception as exc:
            raise _public_tool_error(exc) from exc
        return ExecutionCommandResponse.from_domain(
            result.execution, operation_id=result.operation_id
        )

    @server.tool(
        description="Get the PostgreSQL-backed current execution state."
    )
    async def execution_get(execution_id: UUID) -> ExecutionResponse:
        try:
            execution = await _trace_call(
                tracing,
                "executor.mcp.execution_get",
                (
                    execution_queries.execution(execution_id)
                    if execution_queries is not None
                    else execution_service.get(execution_id)
                ),
                {"executor.execution.id": str(execution_id)},
            )
        except Exception as exc:
            raise _public_tool_error(exc) from exc
        return ExecutionResponse.from_view(execution)

    @server.tool(
        description=(
            "Request cancellation without waiting for worker acknowledgement. "
            "A successful call transitions a non-terminal execution to CANCEL_REQUESTED."
        )
    )
    async def execution_cancel(
        request: ExecutionCancelRequest,
    ) -> ExecutionCommandResponse:
        try:
            execution = await _trace_call(
                tracing,
                "executor.mcp.execution_cancel",
                execution_service.cancel(
                    CancelExecutionCommand(
                        execution_id=request.execution_id,
                        idempotency_key=request.idempotency_key,
                        reason=request.reason,
                        actor_type=request.actor.type,
                        actor_id=request.actor.id,
                    )
                ),
                {"executor.execution.id": str(request.execution_id)},
            )
        except Exception as exc:
            raise _public_tool_error(exc) from exc
        return ExecutionCommandResponse.from_domain(execution)

    @server.tool(
        description=(
            "Resume a FAILED execution from its failed step using the retained Runtime session. "
            "The call queues a new attempt and returns immediately."
        )
    )
    async def execution_retry(
        request: ExecutionRetryRequest,
    ) -> ExecutionCommandResponse:
        try:
            result = await _trace_call(
                tracing,
                "executor.mcp.execution_retry",
                execution_service.retry_result(
                    RetryExecutionCommand(
                        execution_id=request.execution_id,
                        idempotency_key=request.idempotency_key,
                        actor_type=request.actor.type,
                        actor_id=request.actor.id,
                    )
                ),
                {"executor.execution.id": str(request.execution_id)},
            )
        except Exception as exc:
            raise _public_tool_error(exc) from exc
        return ExecutionCommandResponse.from_domain(
            result.execution, operation_id=result.operation_id
        )

    @server.tool(
        description=(
            "Append and queue one or more consecutive Steps as the next Operation for a "
            "waiting MULTI execution. "
            "expected_version prevents stale Agent decisions from being accepted."
        )
    )
    async def execution_operation_create(
        request: ExecutionOperationCreateRequest,
    ) -> ExecutionCommandResponse:
        try:
            if execution_spec_resolver is None:
                raise ToolError(
                    f"[{ErrorCode.INTERNAL_ERROR}] ExecutionSpec resolver is not configured."
                )
            resolved = await execution_spec_resolver.resolve(request.spec)
            source_steps = resolved.steps
            result = await _trace_call(
                tracing,
                "executor.mcp.execution_operation_create",
                execution_service.create_operation_result(
                    CreateOperationCommand(
                        execution_id=request.execution_id,
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
                    "executor.execution.id": str(request.execution_id),
                    "executor.operation.first_sequence": source_steps[
                        0
                    ].sequence,
                    "executor.operation.last_sequence": source_steps[
                        -1
                    ].sequence,
                },
            )
        except Exception as exc:
            raise _public_tool_error(exc) from exc
        return ExecutionCommandResponse.from_domain(
            result.execution, operation_id=result.operation_id
        )

    @server.tool(
        description=(
            "Finalize a waiting MULTI execution and release its retained Runtime session."
        )
    )
    async def execution_finalize(
        request: ExecutionFinalizeRequest,
    ) -> ExecutionCommandResponse:
        try:
            execution = await _trace_call(
                tracing,
                "executor.mcp.execution_finalize",
                execution_service.finalize_execution(
                    FinalizeExecutionCommand(
                        execution_id=request.execution_id,
                        idempotency_key=request.idempotency_key,
                        expected_version=request.expected_version,
                        actor_type=request.actor.type,
                        actor_id=request.actor.id,
                    )
                ),
                {"executor.execution.id": str(request.execution_id)},
            )
        except Exception as exc:
            raise _public_tool_error(exc) from exc
        return ExecutionCommandResponse.from_domain(execution)

    if notebook_queries is not None:

        @server.tool(
            description=(
                "Read the Runtime-owned Execution notebook. SUMMARY returns source previews and "
                "output summaries. FULL returns complete source and outputs for the requested "
                "page. start_index and limit provide bounded index pagination."
            )
        )
        async def execution_notebook_read(
            execution_id: UUID,
            view: NotebookResponseFormat = "SUMMARY",
            start_index: Annotated[int, Field(ge=0)] = 0,
            limit: Annotated[int, Field(ge=1, le=200)] = 20,
        ) -> ExecutionNotebookResponse:
            try:
                notebook = await notebook_queries.read_notebook(
                    execution_id,
                    view=view,
                    start_index=start_index,
                    limit=limit,
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return ExecutionNotebookResponse.from_view(notebook)

        @server.tool(
            description=(
                "Read one Runtime-owned Notebook cell with complete source and all current "
                "outputs."
            ),
            structured_output=False,
        )
        async def execution_notebook_cell_read(
            execution_id: UUID,
            cell_index: Annotated[int, Field(ge=0)],
        ) -> list[TextContent | ImageContent]:
            try:
                view = await notebook_queries.read_cell(
                    execution_id, cell_index
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return _notebook_cell_content(view)

    if execution_queries is not None:
        if materialized_artifacts is not None:

            @server.tool(
                description=(
                    "Materialize Agent-authored UTF-8 text from INLINE content or an Executor "
                    "input-PV PATH as a Runtime-owned Execution Artifact. REPORT files are "
                    "written below reports/ and may also be appended to the notebook."
                )
            )
            async def execution_artifact_create(
                request: ExecutionArtifactMaterializeToolRequest,
            ) -> ExecutionArtifactResponse:
                try:
                    artifact_id = await materialized_artifacts.materialize(
                        request.to_command(request.execution_id)
                    )
                    view = await execution_queries.artifact(artifact_id)
                except Exception as exc:
                    raise _public_tool_error(exc) from exc
                return ExecutionArtifactResponse.from_view(view)

        if execution_results is not None:

            @server.tool(
                description=(
                    "Get the compact authoritative Execution result after an Executor event "
                    "signals availability. Includes current Operation and Step result "
                    "references, Attempt summaries, and Artifact summaries."
                )
            )
            async def execution_result_get(
                execution_id: UUID,
            ) -> ExecutionResultResponse:
                try:
                    bundle = await execution_results.execution(execution_id)
                except Exception as exc:
                    raise _public_tool_error(exc) from exc
                return ExecutionResultResponse.from_bundle(bundle)

            @server.tool(
                description=(
                    "Get one Operation together with all current Step results after an "
                    "operation result event is received."
                )
            )
            async def execution_operation_result_get(
                execution_id: UUID,
                operation_id: UUID,
            ) -> ExecutionOperationResultResponse:
                try:
                    bundle = await execution_results.operation(
                        execution_id, operation_id
                    )
                except Exception as exc:
                    raise _public_tool_error(exc) from exc
                return ExecutionOperationResultResponse.from_bundle(bundle)

        @server.tool(
            description=(
                "List executions using an opaque cursor. Pass the returned "
                "next_cursor unchanged as cursor to continue. Optional filters remain fixed "
                "across pages."
            )
        )
        async def execution_list(
            user_id: str | None = None,
            project_id: str | None = None,
            session_id: str | None = None,
            task_id: str | None = None,
            workflow_id: str | None = None,
            status: ExecutionStatus | None = None,
            cursor: str | None = None,
            limit: StandardLimit = 100,
        ) -> ExecutionPageResponse:
            try:
                page = await execution_queries.executions(
                    user_id=user_id,
                    project_id=project_id,
                    session_id=session_id,
                    task_id=task_id,
                    workflow_id=workflow_id,
                    status=status,
                    cursor=cursor,
                    limit=limit,
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return ExecutionPageResponse.from_page(page)

        @server.tool(
            description=(
                "List current execution Steps using an opaque MCP-style cursor. Pass an exact "
                "returned next_cursor as cursor to continue; never parse or modify it."
            )
        )
        async def execution_step_list(
            execution_id: UUID,
            cursor: str | None = None,
            limit: StandardLimit = 100,
        ) -> ExecutionStepPageResponse:
            try:
                page = await execution_queries.steps(
                    execution_id,
                    cursor=cursor,
                    limit=limit,
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return ExecutionStepPageResponse.from_page(page, execution_id)

        @server.tool(
            description=(
                "List Agent-submitted execution Operations using an opaque cursor. An Operation "
                "is one accepted batch of consecutive Steps."
            )
        )
        async def execution_operation_list(
            execution_id: UUID,
            cursor: str | None = None,
            limit: StandardLimit = 100,
        ) -> ExecutionOperationPageResponse:
            try:
                page = await execution_queries.operations(
                    execution_id, cursor=cursor, limit=limit
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return ExecutionOperationPageResponse.from_page(page)

        @server.tool(
            description="Get one accepted execution Operation detail."
        )
        async def execution_operation_get(
            execution_id: UUID,
            operation_id: UUID,
        ) -> ExecutionOperationResponse:
            try:
                view = await execution_queries.operation(
                    execution_id, operation_id
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return ExecutionOperationResponse.from_view(view)

        @server.tool(
            description="List current Step results belonging to one Operation."
        )
        async def execution_operation_step_list(
            execution_id: UUID,
            operation_id: UUID,
            cursor: str | None = None,
            limit: StandardLimit = 100,
        ) -> ExecutionStepPageResponse:
            try:
                page = await execution_queries.operation_steps(
                    execution_id, operation_id, cursor=cursor, limit=limit
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return ExecutionStepPageResponse.from_page(page, execution_id)

        @server.tool(
            description=(
                "List immutable execution Attempt summaries with outcome and Step count."
            )
        )
        async def execution_attempt_list(
            execution_id: UUID,
            cursor: str | None = None,
            limit: StandardLimit = 100,
        ) -> ExecutionAttemptPageResponse:
            try:
                page = await execution_queries.attempts(
                    execution_id,
                    cursor=cursor,
                    limit=limit,
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return ExecutionAttemptPageResponse.from_page(page)

        @server.tool(
            description="Get one immutable execution Attempt in detail."
        )
        async def execution_attempt_get(
            execution_id: UUID,
            attempt_id: UUID,
        ) -> ExecutionAttemptDetailResponse:
            try:
                view = await execution_queries.attempt(
                    execution_id, attempt_id
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return ExecutionAttemptDetailResponse.from_view(view)

        @server.tool(
            description=(
                "List immutable Step results for one Attempt using an opaque cursor."
            )
        )
        async def execution_attempt_step_list(
            execution_id: UUID,
            attempt_id: UUID,
            cursor: str | None = None,
            limit: StandardLimit = 100,
        ) -> ExecutionStepAttemptPageResponse:
            try:
                page = await execution_queries.attempt_steps(
                    execution_id,
                    attempt_id,
                    cursor=cursor,
                    limit=limit,
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return ExecutionStepAttemptPageResponse.from_page(page)

        @server.tool(
            description=(
                "List the PostgreSQL Outbox event timeline and Redis publication state for an "
                "execution."
            )
        )
        async def execution_event_list(
            execution_id: UUID,
            cursor: str | None = None,
            limit: EventLimit = 200,
        ) -> ExecutionEventPageResponse:
            try:
                page = await execution_queries.events(
                    execution_id,
                    cursor=cursor,
                    limit=limit,
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return ExecutionEventPageResponse.from_page(page)

        @server.tool(
            description=(
                "List Artifacts produced by an execution with Attempt, Step, storage, checksum, "
                "and lineage references."
            )
        )
        async def execution_artifact_list(
            execution_id: UUID,
            cursor: str | None = None,
            limit: ArtifactLimit = 100,
        ) -> ExecutionArtifactPageResponse:
            try:
                page = await execution_queries.artifacts(
                    execution_id,
                    cursor=cursor,
                    limit=limit,
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return ExecutionArtifactPageResponse.from_page(page)

        @server.tool(
            description="Get one Execution Artifact and its direct lineage references."
        )
        async def execution_artifact_get(
            artifact_id: UUID,
        ) -> ExecutionArtifactResponse:
            try:
                view = await execution_queries.artifact(artifact_id)
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return ExecutionArtifactResponse.from_view(view)

    if runtime_manager is not None:

        @server.tool(
            description=(
                "Register or update a Runtime Target. The credential is encrypted at rest and "
                "never returned. The target is probed immediately and becomes schedulable only "
                "healthy."
            )
        )
        async def runtime_target_upsert(
            request: RuntimeTargetUpsertRequest,
        ) -> RuntimeTargetResponse:
            try:
                view = await runtime_manager.upsert(
                    UpsertRuntimeTargetCommand(
                        idempotency_key=request.idempotency_key,
                        name=request.name,
                        runtime_type=request.runtime_type,
                        connection_config=request.connection_config,
                        credential=(
                            request.credential.get_secret_value()
                            if request.credential
                            else None
                        ),
                        pool=request.pool,
                        actor_type=request.actor.type,
                        actor_id=request.actor.id,
                        max_concurrent_executions=request.max_concurrent_executions,
                    )
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return RuntimeTargetResponse.from_view(view)

        @server.tool(
            description="List registered Runtime Targets and current capacity state."
        )
        async def runtime_target_list(
            pool: RuntimePool | None = None,
            runtime_type: RuntimeType | None = None,
            status: RuntimeTargetStatus | None = None,
            enabled: bool | None = None,
            cursor: str | None = None,
            limit: StandardLimit = 100,
        ) -> RuntimeTargetPageResponse:
            try:
                page = await runtime_manager.list(
                    pool,
                    runtime_type=runtime_type,
                    status=status,
                    enabled=enabled,
                    cursor=cursor,
                    limit=limit,
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return RuntimeTargetPageResponse.from_page(page)

        @server.tool(
            description="Get one registered Runtime Target without exposing its credential."
        )
        async def runtime_target_get(target_id: UUID) -> RuntimeTargetResponse:
            try:
                view = await runtime_manager.get(target_id)
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return RuntimeTargetResponse.from_view(view)

        @server.tool(
            description=(
                "Probe a Runtime Target now and persist its health and supported profiles."
            )
        )
        async def runtime_target_probe(
            request: RuntimeTargetProbeRequest,
        ) -> RuntimeTargetResponse:
            try:
                view = await runtime_manager.probe(
                    request.target_id,
                    actor_type=request.actor.type,
                    actor_id=request.actor.id,
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return RuntimeTargetResponse.from_view(view)

        @server.tool(
            description=(
                "Disable a Runtime Target. It remains queryable, is marked "
                "OFFLINE, and excluded from new scheduling."
            )
        )
        async def runtime_target_disable(
            request: RuntimeTargetDisableRequest,
        ) -> RuntimeTargetResponse:
            try:
                view = await runtime_manager.disable(
                    DisableRuntimeTargetCommand(
                        idempotency_key=request.idempotency_key,
                        target_id=request.target_id,
                        actor_type=request.actor.type,
                        actor_id=request.actor.id,
                    )
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return RuntimeTargetResponse.from_view(view)

        @server.tool(
            description=(
                "Set a target to DRAINING to stop new scheduling while current work finishes, "
                "or probe and return it to ACTIVE."
            )
        )
        async def runtime_target_set_state(
            request: RuntimeTargetSetStateRequest,
        ) -> RuntimeTargetResponse:
            try:
                view = await runtime_manager.set_state(
                    SetRuntimeTargetStateCommand(
                        idempotency_key=request.idempotency_key,
                        target_id=request.target_id,
                        desired_state=RuntimeTargetStatus(
                            request.desired_state
                        ),
                        actor_type=request.actor.type,
                        actor_id=request.actor.id,
                    )
                )
            except Exception as exc:
                raise _public_tool_error(exc) from exc
            return RuntimeTargetResponse.from_view(view)

    return server


def _notebook_cell_content(
    view: NotebookCellView,
) -> list[TextContent | ImageContent]:
    header = {
        "index": view.index,
        "id": view.id,
        "type": view.type,
        "execution_count": view.execution_count,
        "metadata": view.metadata,
    }
    content: list[TextContent | ImageContent] = [
        TextContent(type="text", text=json.dumps(header, ensure_ascii=False)),
        TextContent(type="text", text=view.source),
    ]
    for output in view.outputs:
        if output.get("output_type") == "stream":
            content.append(
                TextContent(type="text", text=str(output.get("text", "")))
            )
            continue
        if output.get("output_type") == "error":
            content.append(
                TextContent(
                    type="text", text=json.dumps(output, ensure_ascii=False)
                )
            )
            continue
        data = output.get("data")
        if not isinstance(data, dict):
            content.append(
                TextContent(
                    type="text", text=json.dumps(output, ensure_ascii=False)
                )
            )
            continue
        for mime_type, value in data.items():
            if mime_type in {"image/png", "image/jpeg"} and isinstance(
                value, str
            ):
                content.append(
                    ImageContent(type="image", data=value, mime_type=mime_type)
                )
            else:
                rendered = (
                    value
                    if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False)
                )
                content.append(
                    TextContent(type="text", text=f"[{mime_type}]\n{rendered}")
                )
    return content
