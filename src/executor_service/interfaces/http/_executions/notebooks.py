"""Runtime-owned Execution notebook query routes."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path

from executor_service.application.notebook_queries import (
    NotebookResponseFormat,
)
from executor_service.container import ApplicationContainer
from executor_service.interfaces.contracts import (
    ExecutionNotebookCellResponse,
    ExecutionNotebookResponse,
)
from executor_service.interfaces.http._executions.common import (
    DOMAIN_ERROR_RESPONSES,
    NotebookLimit,
    NotebookStartIndex,
    execution_router,
)


def build_notebook_router(container: ApplicationContainer) -> APIRouter:
    router = execution_router()

    @router.get(
        "/executions/{execution_id}/notebook",
        response_model=ExecutionNotebookResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Read Runtime-owned execution notebook cells",
    )
    async def read_execution_notebook(
        execution_id: UUID,
        view: NotebookResponseFormat = "SUMMARY",
        start_index: NotebookStartIndex = 0,
        limit: NotebookLimit = 20,
    ) -> ExecutionNotebookResponse:
        notebook = await container.notebook_queries.read_notebook(
            execution_id,
            view=view,
            start_index=start_index,
            limit=limit,
        )
        return ExecutionNotebookResponse.from_view(notebook)

    @router.get(
        "/executions/{execution_id}/notebook/cells/{cell_index}",
        response_model=ExecutionNotebookCellResponse,
        responses=DOMAIN_ERROR_RESPONSES,
        summary="Read one Runtime-owned execution notebook cell",
    )
    async def read_execution_notebook_cell(
        execution_id: UUID,
        cell_index: Annotated[int, Path(ge=0)],
    ) -> ExecutionNotebookCellResponse:
        view = await container.notebook_queries.read_cell(
            execution_id, cell_index
        )
        return ExecutionNotebookCellResponse.from_view(execution_id, view)

    return router
