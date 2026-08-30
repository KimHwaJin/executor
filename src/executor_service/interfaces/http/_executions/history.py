"""Execution Step, Operation, Attempt, and event history routes."""

from uuid import UUID

from fastapi import APIRouter

from executor_service.container import ApplicationContainer
from executor_service.interfaces.contracts import (
    ExecutionAttemptDetailResponse,
    ExecutionAttemptPageResponse,
    ExecutionEventPageResponse,
    ExecutionOperationPageResponse,
    ExecutionOperationResponse,
    ExecutionOperationResultResponse,
    ExecutionStepAttemptPageResponse,
    ExecutionStepPageResponse,
    ExecutionStepResponse,
)
from executor_service.interfaces.http._executions.common import (
    DOMAIN_ERROR_RESPONSES,
    AttemptLimit,
    Cursor,
    EventLimit,
    EventSequence,
    ExecutionLimit,
    execution_router,
)


def build_history_router(container: ApplicationContainer) -> APIRouter:
    router = execution_router()
    execution_queries = container.execution_queries
    execution_results = container.execution_results

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
            execution_id,
            operation_id,
            cursor=cursor,
            limit=limit,
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

    return router
