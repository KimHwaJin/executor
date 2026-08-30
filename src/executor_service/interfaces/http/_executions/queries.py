"""Execution summary and result query routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from executor_service.container import ApplicationContainer
from executor_service.domain.enums import ExecutionStatus
from executor_service.interfaces.contracts import (
    ExecutionPageResponse,
    ExecutionResponse,
    ExecutionResultResponse,
)
from executor_service.interfaces.http._executions.common import (
    DOMAIN_ERROR_RESPONSES,
    Cursor,
    ExecutionLimit,
    execution_router,
    trace_call,
)


def build_query_router(container: ApplicationContainer) -> APIRouter:
    router = execution_router()
    execution_queries = container.execution_queries
    execution_results = container.execution_results
    tracing = container.tracing

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
        execution = await trace_call(
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
        bundle = await trace_call(
            tracing,
            "executor.http.execution_result_get",
            execution_results.execution(execution_id),
            {"executor.execution.id": str(execution_id)},
        )
        return ExecutionResultResponse.from_bundle(bundle)

    return router
