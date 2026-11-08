"""Execution summary and result query routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from executor_service.container import ApplicationContainer
from executor_service.domain.enums import ExecutionStatus, TriggerType
from executor_service.domain.errors import ForbiddenError
from executor_service.interfaces.auth import USER_ID
from executor_service.interfaces.contracts import (
    ExecutionPageResponse,
    ExecutionResponse,
    ExecutionResultResponse,
)
from executor_service.interfaces.http.executions.common import (
    DOMAIN_ERROR_RESPONSES,
    CurrentCaller,
    Cursor,
    ExecutionLimit,
    ScopeFilter,
    execution_router,
)

# X-User-Id's format before lowercasing: the owner to filter by.
UserFilter = Annotated[
    str | None,
    Query(pattern=USER_ID.pattern.replace("a-z0-9", "A-Za-z0-9")),
]


def build_query_router(container: ApplicationContainer) -> APIRouter:
    router = execution_router()
    execution_queries = container.execution_queries
    execution_results = container.execution_results

    @router.get(
        "/executions",
        response_model=ExecutionPageResponse,
        summary="List execution history",
    )
    async def list_executions(
        caller: CurrentCaller,
        user_id: UserFilter = None,
        project_id: ScopeFilter = None,
        session_id: ScopeFilter = None,
        trigger_type: TriggerType | None = None,
        execution_status: Annotated[
            ExecutionStatus | None, Query(alias="status")
        ] = None,
        cursor: Cursor = None,
        limit: ExecutionLimit = 100,
    ) -> ExecutionPageResponse:
        # X-User-Id limits a caller to its own Executions; without it a
        # system caller sees its whole tenant and may filter by owner.
        owner = caller.user_id
        if user_id is not None:
            if owner is not None and user_id.lower() != owner:
                raise ForbiddenError(
                    "user_id must match X-User-Id when both are sent."
                )
            owner = user_id.lower()
        page = await execution_queries.executions(
            user_id=owner,
            project_id=project_id,
            session_id=session_id,
            tenant_id=caller.tenant_id,
            trigger_type=trigger_type,
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
        execution = await execution_queries.execution(execution_id)
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
        bundle = await execution_results.execution(execution_id)
        return ExecutionResultResponse.from_bundle(bundle)

    return router
