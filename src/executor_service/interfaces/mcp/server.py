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
from executor_service.application.runtime_targets import (
    RemoveRuntimeTargetCommand,
    RuntimeTargetManager,
    SetRuntimeTargetStateCommand,
    UpsertRuntimeTargetCommand,
)
from executor_service.application.services import ExecutionService
from executor_service.domain.enums import (
    ExecutionStatus,
    RuntimePool,
    RuntimeTargetStatus,
)
from executor_service.domain.errors import DomainError
from executor_service.interfaces.mcp.execution_specs import ExecutionSpecResolver
from executor_service.interfaces.mcp.schemas import (
    ExecutionArtifactPageResponse,
    ExecutionArtifactResponse,
    ExecutionAttemptPageResponse,
    ExecutionCancelRequest,
    ExecutionContinueRequest,
    ExecutionEventPageResponse,
    ExecutionFinishRequest,
    ExecutionPageResponse,
    ExecutionResponse,
    ExecutionRetryRequest,
    ExecutionStepPageResponse,
    ExecutionSubmitRequest,
    ExecutionTraceResponse,
    ExecutorCapabilities,
    RuntimeTargetPageResponse,
    RuntimeTargetProbeRequest,
    RuntimeTargetRemoveRequest,
    RuntimeTargetResponse,
    RuntimeTargetSetStateRequest,
    RuntimeTargetUpsertRequest,
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
    runtime_manager: RuntimeTargetManager | None = None,
    execution_queries: ExecutionQueryService | None = None,
    tracing: TracingManager | None = None,
    execution_spec_resolver: ExecutionSpecResolver | None = None,
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
        if runtime_manager is not None:
            management_tools = (
                "runtime_target_upsert",
                "runtime_target_list",
                "runtime_target_get",
                "runtime_target_probe",
                "runtime_target_remove",
                "runtime_target_set_state",
            )
        query_tools: tuple[str, ...] = ()
        if execution_queries is not None:
            query_tools = (
                "execution_list",
                "execution_step_list",
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
            if execution_spec_resolver is None:
                raise ToolError("ExecutionSpec resolver is not configured.")
            resolved = await execution_spec_resolver.resolve(request.source)
            if resolved.spec.steps[0].sequence != 0:
                raise ToolError(
                    "Execution submit requires an ExecutionSpec starting at sequence 0."
                )
            execution = await _trace_call(
                tracing,
                "executor.mcp.execution_submit",
                execution_service.submit(
                    request.to_command(
                        resolved.spec,
                        source_content=resolved.canonical_content,
                        source_sha256=resolved.sha256,
                    )
                ),
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
                        actor_type=request.actor.type,
                        actor_id=request.actor.id,
                    )
                ),
                {"executor.execution.id": str(request.execution_id)},
            )
        except DomainError as exc:
            raise ToolError(str(exc)) from exc
        return ExecutionResponse.from_domain(execution)

    @server.tool(
        description=(
            "Resume a FAILED execution from its failed step using the retained Runtime session. "
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
                        actor_type=request.actor.type,
                        actor_id=request.actor.id,
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
            if execution_spec_resolver is None:
                raise ToolError("ExecutionSpec resolver is not configured.")
            resolved = await execution_spec_resolver.resolve(request.source)
            if len(resolved.spec.steps) != 1:
                raise ToolError("DYNAMIC continue requires exactly one ExecutionSpec step.")
            source_step = resolved.spec.steps[0]
            execution = await _trace_call(
                tracing,
                "executor.mcp.execution_continue",
                execution_service.continue_execution(
                    ContinueExecutionCommand(
                        execution_id=request.execution_id,
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
                        actor_type=request.actor.type,
                        actor_id=request.actor.id,
                    )
                ),
                {
                    "executor.execution.id": str(request.execution_id),
                    "executor.step.sequence": source_step.sequence,
                },
            )
        except DomainError as exc:
            raise ToolError(str(exc)) from exc
        return ExecutionResponse.from_domain(execution)

    @server.tool(
        description=(
            "Finalize a waiting DYNAMIC execution, persist its notebook, and stop its "
            "Runtime session."
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
                        actor_type=request.actor.type,
                        actor_id=request.actor.id,
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
                "List executions using an opaque MCP-style cursor. Pass the returned "
                "nextCursor unchanged as cursor to continue. Optional filters remain fixed "
                "across pages."
            )
        )
        async def execution_list(
            requested_by_user_id: str | None = None,
            project_id: str | None = None,
            session_id: str | None = None,
            task_id: str | None = None,
            status: ExecutionStatus | None = None,
            cursor: str | None = None,
            limit: int = 100,
        ) -> ExecutionPageResponse:
            try:
                page = await execution_queries.executions(
                    requested_by_user_id=requested_by_user_id,
                    project_id=project_id,
                    session_id=session_id,
                    task_id=task_id,
                    status=status,
                    cursor=cursor,
                    limit=max(1, min(limit, 200)),
                )
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return ExecutionPageResponse.from_page(page)

        @server.tool(
            description=(
                "List current execution Steps using an opaque MCP-style cursor. Pass an exact "
                "returned nextCursor as cursor to continue; never parse or modify it."
            )
        )
        async def execution_step_list(
            execution_id: UUID,
            cursor: str | None = None,
            limit: int = 100,
        ) -> ExecutionStepPageResponse:
            try:
                page = await execution_queries.steps(
                    execution_id,
                    cursor=cursor,
                    limit=max(1, min(limit, 200)),
                )
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return ExecutionStepPageResponse.from_page(page)

        @server.tool(
            description=(
                "List immutable execution Attempts with the Step results recorded in each attempt."
            )
        )
        async def execution_attempt_list(
            execution_id: UUID,
            cursor: str | None = None,
            limit: int = 100,
        ) -> ExecutionAttemptPageResponse:
            try:
                page = await execution_queries.attempts(
                    execution_id,
                    cursor=cursor,
                    limit=max(1, min(limit, 200)),
                )
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return ExecutionAttemptPageResponse.from_page(page)

        @server.tool(
            description=(
                "List the PostgreSQL Outbox event timeline and Redis publication state for an "
                "execution."
            )
        )
        async def execution_event_list(
            execution_id: UUID,
            cursor: str | None = None,
            limit: int = 200,
        ) -> ExecutionEventPageResponse:
            try:
                page = await execution_queries.events(
                    execution_id,
                    cursor=cursor,
                    limit=max(1, min(limit, 500)),
                )
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return ExecutionEventPageResponse.from_page(page)

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
            execution_id: UUID,
            cursor: str | None = None,
            limit: int = 500,
        ) -> ExecutionArtifactPageResponse:
            try:
                page = await execution_queries.artifacts(
                    execution_id,
                    cursor=cursor,
                    limit=max(1, min(limit, 1000)),
                )
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return ExecutionArtifactPageResponse.from_page(page)

        @server.tool(description="Get one Execution Artifact and its direct lineage references.")
        async def execution_artifact_get(artifact_id: UUID) -> ExecutionArtifactResponse:
            try:
                view = await execution_queries.artifact(artifact_id)
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
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
                            request.credential.get_secret_value() if request.credential else None
                        ),
                        pool=request.pool,
                        actor_type=request.actor.type,
                        actor_id=request.actor.id,
                        max_concurrent_executions=request.max_concurrent_executions,
                    )
                )
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return RuntimeTargetResponse.from_view(view)

        @server.tool(description="List registered Runtime Targets and current capacity state.")
        async def runtime_target_list(
            pool: RuntimePool | None = None,
            cursor: str | None = None,
            limit: int = 100,
        ) -> RuntimeTargetPageResponse:
            page = await runtime_manager.list(pool, cursor=cursor, limit=max(1, min(limit, 200)))
            return RuntimeTargetPageResponse.from_page(page)

        @server.tool(
            description="Get one registered Runtime Target without exposing its credential."
        )
        async def runtime_target_get(target_id: UUID) -> RuntimeTargetResponse:
            try:
                view = await runtime_manager.get(target_id)
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
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
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return RuntimeTargetResponse.from_view(view)

        @server.tool(
            description=(
                "Soft-remove a Runtime Target. It remains queryable but is disabled, marked "
                "OFFLINE, and excluded from new scheduling."
            )
        )
        async def runtime_target_remove(
            request: RuntimeTargetRemoveRequest,
        ) -> RuntimeTargetResponse:
            try:
                view = await runtime_manager.remove(
                    RemoveRuntimeTargetCommand(
                        idempotency_key=request.idempotency_key,
                        target_id=request.target_id,
                        actor_type=request.actor.type,
                        actor_id=request.actor.id,
                    )
                )
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
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
                        desired_state=RuntimeTargetStatus(request.desired_state),
                        actor_type=request.actor.type,
                        actor_id=request.actor.id,
                    )
                )
            except DomainError as exc:
                raise ToolError(str(exc)) from exc
            return RuntimeTargetResponse.from_view(view)

    return server
