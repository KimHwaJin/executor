"""Execution lifecycle command routes."""

from uuid import UUID

from fastapi import APIRouter, Response, status

from executor_service.application.commands import (
    CancelExecutionCommand,
    CreateOperationCommand,
    FinalizeExecutionCommand,
    RetryExecutionCommand,
    StepSpec,
)
from executor_service.container import ApplicationContainer
from executor_service.domain.errors import InvalidExecutionSpecError
from executor_service.interfaces.contracts import (
    ExecutionCommandResponse,
    ExecutionSubmitRequest,
)
from executor_service.interfaces.http._executions.common import (
    DOMAIN_ERROR_RESPONSES,
    execution_router,
    trace_call,
)
from executor_service.interfaces.http.schemas import (
    ExecutionCancelRequest,
    ExecutionFinalizeRequest,
    ExecutionOperationCreateRequest,
    ExecutionRetryRequest,
)


def build_command_router(container: ApplicationContainer) -> APIRouter:
    router = execution_router()
    execution_service = container.execution_service
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
                "Execution submit requires an ExecutionSpec starting at "
                "sequence 0."
            )
        result = await trace_call(
            tracing,
            "executor.http.execution_submit",
            execution_service.submit_result(request.to_command(resolved)),
        )
        execution = result.execution
        response.headers["Location"] = f"/api/v1/executions/{execution.id}"
        return ExecutionCommandResponse.from_domain(
            execution, operation_id=result.operation_id
        )

    @router.post(
        "/executions/{execution_id}/cancel",
        response_model=ExecutionCommandResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Request asynchronous cancellation",
    )
    async def cancel_execution(
        execution_id: UUID,
        request: ExecutionCancelRequest,
        response: Response,
    ) -> ExecutionCommandResponse:
        execution = await trace_call(
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
        execution_id: UUID,
        request: ExecutionRetryRequest,
        response: Response,
    ) -> ExecutionCommandResponse:
        result = await trace_call(
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
        result = await trace_call(
            tracing,
            "executor.http.execution_operation_create",
            execution_service.create_operation_result(
                CreateOperationCommand(
                    execution_id=execution_id,
                    idempotency_key=request.idempotency_key,
                    expected_version=request.expected_version,
                    spec_schema_version=resolved.spec.schema_version,
                    operation_timeout_seconds=(
                        request.operation_timeout_seconds
                    ),
                    metadata=request.metadata,
                    steps=tuple(
                        StepSpec(
                            sequence=source_step.sequence,
                            code=source_step.content,
                            source_type=source_step.source_type,
                            source_path=source_step.source_path,
                            source_sha256=source_step.source_sha256,
                            step_timeout_seconds=(
                                source_step.step_timeout_seconds
                            ),
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
                "executor.operation.first_sequence": (
                    source_steps[0].sequence
                ),
                "executor.operation.last_sequence": (
                    source_steps[-1].sequence
                ),
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
        execution = await trace_call(
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

    return router
