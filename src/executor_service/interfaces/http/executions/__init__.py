"""Versioned Execution REST routes."""

from fastapi import APIRouter

from executor_service.container import ApplicationContainer
from executor_service.interfaces.http.executions.artifacts import (
    build_artifact_router,
)
from executor_service.interfaces.http.executions.commands import (
    build_command_router,
)
from executor_service.interfaces.http.executions.history import (
    build_history_router,
)
from executor_service.interfaces.http.executions.notebooks import (
    build_notebook_router,
)
from executor_service.interfaces.http.executions.queries import (
    build_query_router,
)


def build_execution_router(container: ApplicationContainer) -> APIRouter:
    router = APIRouter()
    router.include_router(build_command_router(container))
    router.include_router(build_query_router(container))
    router.include_router(build_notebook_router(container))
    router.include_router(build_history_router(container))
    router.include_router(build_artifact_router(container))
    return router


__all__ = [
    "build_artifact_router",
    "build_command_router",
    "build_history_router",
    "build_notebook_router",
    "build_query_router",
]
