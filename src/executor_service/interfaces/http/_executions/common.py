"""Shared FastAPI definitions for Execution REST routes."""

from collections.abc import Awaitable
from typing import Annotated, Any

from fastapi import APIRouter, Query

from executor_service.interfaces.http.schemas import ErrorResponse
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


def execution_router() -> APIRouter:
    return APIRouter(prefix="/api/v1", tags=["executions"])


async def trace_call[T](
    tracing: TracingManager,
    name: str,
    operation: Awaitable[T],
    attributes: dict[str, object] | None = None,
) -> T:
    with tracing.span(name, attributes=attributes):
        return await operation
