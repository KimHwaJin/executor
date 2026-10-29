"""Shared FastAPI definitions for Execution REST routes."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from executor_service.domain.errors import (
    ExecutionArtifactNotFoundError,
    ExecutionNotFoundError,
)
from executor_service.interfaces.auth import (
    READ_METHODS,
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


async def authorized_caller(request: Request) -> Caller:
    """Apply the tenant and user rule to the Execution or Artifact in the path."""

    caller = current_caller()
    queries = request.app.state.container.execution_queries
    write = request.method not in READ_METHODS
    execution_id = _path_uuid(request, "execution_id")
    artifact_id = _path_uuid(request, "artifact_id")
    if execution_id is not None:
        await authorize_execution(queries, caller, execution_id, write=write)
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


CurrentCaller = Annotated[Caller, Depends(authorized_caller)]


def execution_router() -> APIRouter:
    return APIRouter(
        prefix="/api/v1",
        tags=["executions"],
        dependencies=[
            Depends(documented_identity),
            Depends(authorized_caller),
        ],
    )
