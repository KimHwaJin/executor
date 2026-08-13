"""Datalayer-style reads for Runtime-owned execution notebooks."""

import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from executor_service.application.execution_queries import ExecutionQueryService
from executor_service.domain.errors import (
    ExecutionNotebookNotAvailableError,
    NotebookCellNotFoundError,
    NotebookReadError,
)
from executor_service.domain.runtime import RuntimeStorageAccess

NotebookResponseFormat = Literal["brief", "detailed"]


@dataclass(frozen=True, slots=True)
class NotebookCellView:
    index: int
    id: str | None
    type: str
    execution_count: int | None
    source: str
    line_count: int
    metadata: dict[str, Any]
    outputs: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class NotebookView:
    execution_id: UUID
    response_format: NotebookResponseFormat
    metadata: dict[str, Any]
    cells: list[NotebookCellView]
    start_index: int
    limit: int
    total_count: int


class ExecutionNotebookQueryService:
    def __init__(
        self, executions: ExecutionQueryService, runtime_storage: RuntimeStorageAccess
    ) -> None:
        self._executions = executions
        self._runtime_storage = runtime_storage

    async def read_notebook(
        self,
        execution_id: UUID,
        *,
        response_format: NotebookResponseFormat = "brief",
        start_index: int = 0,
        limit: int = 20,
    ) -> NotebookView:
        if start_index < 0 or limit < 0:
            raise NotebookReadError("Notebook pagination values must be non-negative.")
        notebook = await self._load(execution_id)
        cells = _cells(notebook, execution_id)
        end_index = len(cells) if limit == 0 else start_index + limit
        selected = cells[start_index:end_index]
        return NotebookView(
            execution_id=execution_id,
            response_format=response_format,
            metadata=_object(notebook.get("metadata", {})),
            cells=[
                _cell_view(
                    cell,
                    index=start_index + offset,
                    include_outputs=False,
                    response_format=response_format,
                )
                for offset, cell in enumerate(selected)
            ],
            start_index=start_index,
            limit=limit,
            total_count=len(cells),
        )

    async def read_cell(
        self, execution_id: UUID, cell_index: int, *, include_outputs: bool = True
    ) -> NotebookCellView:
        if cell_index < 0:
            raise NotebookCellNotFoundError("Notebook cell index must be non-negative.")
        notebook = await self._load(execution_id)
        cells = _cells(notebook, execution_id)
        if cell_index >= len(cells):
            raise NotebookCellNotFoundError(
                f"Cell index {cell_index} was not found in Execution {execution_id} notebook."
            )
        return _cell_view(
            cells[cell_index],
            index=cell_index,
            include_outputs=include_outputs,
            response_format="detailed",
        )

    async def _load(self, execution_id: UUID) -> dict[str, Any]:
        execution = await self._executions.execution(execution_id)
        if not execution.notebook_path:
            raise ExecutionNotebookNotAvailableError(
                f"Execution {execution_id} notebook is not available yet."
            )
        try:
            notebook = await self._runtime_storage.read_notebook(
                execution.runtime_type,
                execution.runtime_target_id,
                execution.notebook_path,
            )
        except Exception as exc:
            raise ExecutionNotebookNotAvailableError(
                f"Execution {execution_id} notebook is currently unavailable."
            ) from exc
        return _object(notebook)


def _cells(notebook: dict[str, Any], execution_id: UUID) -> list[dict[str, Any]]:
    cells = notebook.get("cells")
    if not isinstance(cells, list) or not all(isinstance(cell, dict) for cell in cells):
        raise NotebookReadError(f"Execution {execution_id} notebook content is invalid.")
    return cells


def _cell_view(
    cell: dict[str, Any],
    *,
    index: int,
    include_outputs: bool,
    response_format: NotebookResponseFormat,
) -> NotebookCellView:
    source = cell.get("source", "")
    if isinstance(source, list):
        source = "".join(str(line) for line in source)
    source = str(source)
    lines = source.splitlines()
    outputs = cell.get("outputs", []) if include_outputs and cell.get("cell_type") == "code" else []
    if not isinstance(outputs, list):
        raise NotebookReadError("Notebook cell outputs must be an array.")
    return NotebookCellView(
        index=index,
        id=str(cell["id"]) if cell.get("id") is not None else None,
        type=str(cell.get("cell_type", "raw")),
        execution_count=(
            int(cell["execution_count"]) if cell.get("execution_count") is not None else None
        ),
        source=source if response_format == "detailed" else (lines[0] if lines else ""),
        line_count=len(lines),
        metadata=_object(cell.get("metadata", {})),
        outputs=_array(outputs),
    )


def _object(value: Any) -> dict[str, Any]:
    detached = json.loads(json.dumps(value))
    if not isinstance(detached, dict):
        raise NotebookReadError("Notebook value must be an object.")
    return detached


def _array(value: Any) -> list[dict[str, Any]]:
    detached = json.loads(json.dumps(value))
    if not isinstance(detached, list) or not all(isinstance(item, dict) for item in detached):
        raise NotebookReadError("Notebook outputs must contain objects.")
    return detached
