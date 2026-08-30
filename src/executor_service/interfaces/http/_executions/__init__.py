"""Internal Execution REST router builders."""

from executor_service.interfaces.http._executions.artifacts import (
    build_artifact_router,
)
from executor_service.interfaces.http._executions.commands import (
    build_command_router,
)
from executor_service.interfaces.http._executions.history import (
    build_history_router,
)
from executor_service.interfaces.http._executions.notebooks import (
    build_notebook_router,
)
from executor_service.interfaces.http._executions.queries import (
    build_query_router,
)

__all__ = [
    "build_artifact_router",
    "build_command_router",
    "build_history_router",
    "build_notebook_router",
    "build_query_router",
]
