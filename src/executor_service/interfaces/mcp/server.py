"""Official MCP Python SDK v2 server and tool registration."""

from collections.abc import Awaitable
from uuid import UUID

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from executor_service.application.commands import (
    CancelExecutionCommand,
    ContinueExecutionCommand,
    FinishExecutionCommand,
    RetryExecutionCommand,
    StepSpec,
)
from executor_service.application.execution_queries import ExecutionQueryService
from executor_service.application.jupyter_servers import (
    JupyterServerManager,
    RemoveJupyterServerCommand,
    SetJupyterServerStateCommand,
    UpsertJupyterServerCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import JupyterPool, JupyterServerStatus
from executor_service.domain.errors import DomainError
from executor_service.interfaces.mcp.schemas import (
    ExecutionArtifactResponse,
    ExecutionAttemptResponse,
    ExecutionCancelRequest,
    ExecutionContinueRequest,
    ExecutionEventResponse,
    ExecutionFinishRequest,
    ExecutionResponse,
    ExecutionRetryRequest,
    ExecutionSubmitRequest,
    ExecutionTraceResponse,
    ExecutorCapabilities,
    JupyterServerRemoveRequest,
    JupyterServerResponse,
    JupyterServerSetStateRequest,
    JupyterServerUpsertRequest,
)
from executor_service.tracing import TracingManager


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
    jupyter_manager: JupyterServerManager | None = None,
    execution_queries: ExecutionQueryService | None = None,
    tracing: TracingManager | None = None,
) -> MCPServer:
    server = MCPServer(
        name="executor-service",
        version="0.1.0",
        instructions=(
            "Submit asynchronous code executions and use execution_id to query or cancel them. "
            "Tool completion does not mean the submitted execution has finished."
        ),
    )

    @server.tool(description="Return executor protocol and runtime capabilities.")
    async def executor_get_capabilities() -> ExecutorCapabilities:
        management_tools: tuple[str, ...] = ()
        if jupyter_manager is not None:
            management_tools = (
                "jupyter_server_upsert",
                "jupyter_server_list",
                "jupyter_server_get",
                "jupyter_server_probe",
                "jupyter_server_remove",
                "jupyter_server_set_state",
            )
        query_tools: tuple[str, ...] = ()
        if execution_queries is not None:
            query_tools = (
                "execution_attempt_list",
                "execution_event_list",
                "execution_trace_get",
                "execution_artifact_list",
                "execution_artifact_get",
            )
        return ExecutorCapabilities(
            tools=ExecutorCapabilities().tools + management_tools + query_tools
        )

    @server.tool(
        description=(
            "Persist an asynchronous execution request and return immediately with execution_id. "
            "Reusing idempotency_key with the same request returns the original execution."
        )
    )
    async def execution_submit(request: ExecutionSubmitRequest) -> ExecutionResponse:
        try:
            execution = await _trace_call(
                tracing,
                "executor.mcp.execution_submit",
                execution_service.submit(request.to_command()),
            )
        except DomainError as exc:
            raise ToolError(str(exc)) from exc
        return ExecutionResponse.from_domain(execution)

    @server.tool(description="Get the PostgreSQL-backed current execution state.")
    async def execution_get(execution_id: UUID) -> ExecutionResponse:
        try:
            execution = await _trace_call(
                tracing,
                "executor.mcp.execution_get",
                execution_service.get(execution_id),
                {"executor.execution.id": str(execution_id)},
            )
        except DomainError as exc:
            raise ToolError(str(exc)) from exc
        return ExecutionResponse.from_domain(execution)

    @server.tool(
        description=(
            "Request cancellation without waiting for worker acknowledgement. "
            "A successful call transitions a non-terminal execution to CANCEL_REQUESTED."
        )
    )
    async def execution_cancel(request: ExecutionCancelRequest) -> ExecutionResponse:
        try:
            execution = await _trace_call(
                tracing,
                "executor.mcp.execution_cancel",
                execution_service.cancel(
                    CancelExecutionCommand(
                        execution_id=request.execution_id,
                        idempotency_key=request.idempotency_key,
                        reason=request.reason,
                    )
                ),
                {"executor.execution.id": str(request.execution_id)},
            )
        except DomainError as exc:
            raise ToolError(str(exc)) from exc
        return ExecutionResponse.from_domain(execution)

    @server.tool(
        description=(
            "Resume a FAILED execution from its failed step using the retained kernel. "
            "The call queues a new attempt and returns immediately."
        )
    )
    async def execution_retry(request: ExecutionRetryRequest) -> ExecutionResponse:
        try:
            execution = await _trace_call(
                tracing,
                "executor.mcp.execution_retry",
                execution_service.retry(
                    RetryExecutionCommand(
                        execution_id=request.execution_id,
                        idempotency_key=request.idempotency_key,
                    )
                ),
                {"executor.execution.id": str(request.execution_id)},
            )
        except DomainError as exc:
            raise ToolError(str(exc)) from exc
        return ExecutionResponse.from_domain(execution)

    @server.tool(
        description=(
            "Append and queue exactly one next cell for a waiting DYNAMIC execution. "
            "expected_version prevents stale Agent decisions from being accepted."
        )
    )
    async def execution_continue(request: ExecutionContinueRequest) -> ExecutionResponse:
        try:
            execution = await _trace_call(
                tracing,
                "executor.mcp.execution_continue",
                execution_service.continue_execution(
                    ContinueExecutionCommand(
                        execution_id=request.execution_id,
                        idempotency_key=request.idempotency_key,
                        expected_version=request.expected_version,
                        step=StepSpec(
                            sequence=request.step.sequence,
                            code=request.step.code,
                            plan_revision_id=request.step.plan_revision_id,
                            skill_name=request.step.skill_name,
                            tool_name=request.step.tool_name,
                            input_parameters=request.step.input_parameters,
                        ),
                    )
                ),
                {
                    "executor.execution.id": str(request.execution_id),
                    "executor.step.sequence": request.step.sequence,
                },
            )
        except DomainError as exc:
            raise ToolError(str(exc)) from exc
        return ExecutionResponse.from_domain(execution)

    @server.tool(
        description=(
            "Finalize a waiting DYNAMIC execution, persist its notebook, and stop its kernel."
        )
    )
    async def execution_finish(request: ExecutionFinishRequest) -> ExecutionResponse:
        try:
            execution = await _trace_call(
                tracing,
                "executor.mcp.execution_finish",
                execution_service.finish_execution(
                    FinishExecutionCommand(
                        execution_id=request.execution_id,
                        idempotency_key=request.idempotency_key,
                        expected_version=request.expected_version,
                    )
                ),
                {"executor.execution.id": str(request.execution_id)},
            )
        except DomainError as exc:
            raise ToolError(str(exc)) from exc
        return ExecutionResponse.from_domain(execution)

    if execution_queries is not None:

        @server.tool(
            description=(
                "List immutable execution Attempts with the Step results recorded in each "
                "attempt."
            )
        )
        async def execution_attempt_list(
            execution_id: UUID, limit: int = 100
        ) -> list[ExecutionAttemptResponse]:
            try:
                views = await execution_queries.attempts(
                    execution_id, limit=max(1, min(limit, 200))
                )
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return [ExecutionAttemptResponse.from_view(view) for view in views]

        @server.tool(
            description=(
                "List the PostgreSQL Outbox event timeline and Redis publication state for an "
                "execution."
            )
        )
        async def execution_event_list(
            execution_id: UUID, limit: int = 200
        ) -> list[ExecutionEventResponse]:
            try:
                views = await execution_queries.events(
                    execution_id, limit=max(1, min(limit, 500))
                )
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return [ExecutionEventResponse.from_view(view) for view in views]

        @server.tool(
            description=(
                "Return the complete execution trace: current state, every Attempt and Step "
                "result, and the Outbox event timeline."
            )
        )
        async def execution_trace_get(execution_id: UUID) -> ExecutionTraceResponse:
            try:
                view = await execution_queries.trace(execution_id)
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return ExecutionTraceResponse.from_view(view)

        @server.tool(
            description=(
                "List Artifacts produced by an execution with Attempt, Step, storage, checksum, "
                "and lineage references."
            )
        )
        async def execution_artifact_list(
            execution_id: UUID, limit: int = 500
        ) -> list[ExecutionArtifactResponse]:
            try:
                views = await execution_queries.artifacts(
                    execution_id, limit=max(1, min(limit, 1000))
                )
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return [ExecutionArtifactResponse.from_view(view) for view in views]

        @server.tool(
            description="Get one Execution Artifact and its direct lineage references."
        )
        async def execution_artifact_get(artifact_id: UUID) -> ExecutionArtifactResponse:
            try:
                view = await execution_queries.artifact(artifact_id)
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return ExecutionArtifactResponse.from_view(view)

    if jupyter_manager is not None:

        @server.tool(
            description=(
                "Register or update a Jupyter server. The token is encrypted at rest and never "
                "returned. The server is probed immediately and becomes schedulable only when "
                "healthy."
            )
        )
        async def jupyter_server_upsert(
            request: JupyterServerUpsertRequest,
        ) -> JupyterServerResponse:
            try:
                view = await jupyter_manager.upsert(
                    UpsertJupyterServerCommand(
                        idempotency_key=request.idempotency_key,
                        name=request.name,
                        endpoint=str(request.endpoint).rstrip("/"),
                        token=(request.token.get_secret_value() if request.token else None),
                        pool=request.pool,
                        max_concurrent_executions=request.max_concurrent_executions,
                    )
                )
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return JupyterServerResponse.from_view(view)

        @server.tool(description="List registered Jupyter servers and current capacity state.")
        async def jupyter_server_list(
            pool: JupyterPool | None = None,
        ) -> list[JupyterServerResponse]:
            views = await jupyter_manager.list(pool)
            return [JupyterServerResponse.from_view(view) for view in views]

        @server.tool(description="Get one registered Jupyter server without exposing its token.")
        async def jupyter_server_get(server_id: UUID) -> JupyterServerResponse:
            try:
                view = await jupyter_manager.get(server_id)
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return JupyterServerResponse.from_view(view)

        @server.tool(description="Probe a Jupyter server now and persist its health and kernels.")
        async def jupyter_server_probe(server_id: UUID) -> JupyterServerResponse:
            try:
                view = await jupyter_manager.probe(server_id)
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return JupyterServerResponse.from_view(view)

        @server.tool(
            description=(
                "Soft-remove a Jupyter server. It remains queryable but is disabled, marked "
                "OFFLINE, and excluded from new scheduling."
            )
        )
        async def jupyter_server_remove(
            request: JupyterServerRemoveRequest,
        ) -> JupyterServerResponse:
            try:
                view = await jupyter_manager.remove(
                    RemoveJupyterServerCommand(
                        idempotency_key=request.idempotency_key,
                        server_id=request.server_id,
                    )
                )
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return JupyterServerResponse.from_view(view)

        @server.tool(
            description=(
                "Set a server to DRAINING to stop new scheduling while current work finishes, "
                "or probe and return it to ACTIVE."
            )
        )
        async def jupyter_server_set_state(
            request: JupyterServerSetStateRequest,
        ) -> JupyterServerResponse:
            try:
                view = await jupyter_manager.set_state(
                    SetJupyterServerStateCommand(
                        idempotency_key=request.idempotency_key,
                        server_id=request.server_id,
                        desired_state=JupyterServerStatus(request.desired_state),
                    )
                )
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return JupyterServerResponse.from_view(view)

    return server
