"""Shared FastAPI definitions for Execution REST routes."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from executor_service.application.execution_queries import (
    ExecutionDetailView,
)
from executor_service.domain.errors import (
    ExecutionArtifactNotFoundError,
    ExecutionNotFoundError,
)
from executor_service.interfaces.auth import (
    READ_METHODS,
    SCOPE_ID,
    Caller,
    authorize_execution,
    current_caller,
    require_tenant,
    require_user,
)
from executor_service.interfaces.http.identity import documented_identity
from executor_service.interfaces.http.schemas import ErrorResponse

ExecutionLimit = Annotated[int, Query(ge=1, le=200)]
AttemptLimit = Annotated[int, Query(ge=1, le=200)]
EventLimit = Annotated[int, Query(ge=1, le=500)]
EventSequence = Annotated[int, Query(ge=0)]
ArtifactLimit = Annotated[int, Query(ge=1, le=1000)]
Cursor = Annotated[str | None, Query(max_length=2048)]
# Same format as X-Project-Id/X-Session-Id: a filter, not the caller's scope.
ScopeFilter = Annotated[str | None, Query(pattern=SCOPE_ID.pattern)]
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


async def authorized_execution(request: Request) -> ExecutionDetailView | None:
    """The Execution in the path, after the tenant and user rule.

    FastAPI runs a dependency once per request, so the router's check and
    a handler that answers with the Execution share one read.
    """

    execution_id = _path_uuid(request, "execution_id")
    if execution_id is None:
        return None
    return await authorize_execution(
        request.app.state.container.execution_queries,
        current_caller(),
        execution_id,
        write=request.method not in READ_METHODS,
    )


async def authorized_caller(
    request: Request,
    execution: Annotated[
        ExecutionDetailView | None, Depends(authorized_execution)
    ],
) -> Caller:
    """Apply the tenant and user rule to the Execution or Artifact in the path."""

    caller = current_caller()
    queries = request.app.state.container.execution_queries
    write = request.method not in READ_METHODS
    artifact_id = _path_uuid(request, "artifact_id")
    if execution is not None:
        pass
    elif artifact_id is not None:
        artifact = await queries.artifact(artifact_id)
        try:
            await authorize_execution(
                queries, caller, artifact.execution_id, write=write
            )
        except ExecutionNotFoundError:
            raise ExecutionArtifactNotFoundError(
                f"Artifact {artifact_id} was not found."
            ) from None
    else:
        require_tenant(caller)
        if write:
            require_user(caller)
    return caller


def _path_uuid(request: Request, name: str) -> UUID | None:
    value = request.path_params.get(name)
    try:
        return None if value is None else UUID(value)
    except ValueError:
        # Route validation rejects the malformed ID before the handler runs.
        return None


async def path_execution(
    execution: Annotated[
        ExecutionDetailView | None, Depends(authorized_execution)
    ],
) -> ExecutionDetailView:
    if execution is None:
        raise ExecutionNotFoundError("The path names no Execution.")
    return execution


CurrentCaller = Annotated[Caller, Depends(authorized_caller)]
PathExecution = Annotated[ExecutionDetailView, Depends(path_execution)]


def execution_router() -> APIRouter:
    return APIRouter(
        prefix="/api/v1",
        tags=["executions"],
        dependencies=[
            Depends(documented_identity),
            Depends(authorized_caller),
        ],
    )
