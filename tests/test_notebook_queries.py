from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from mcp.types import ImageContent, TextContent

from executor_service.application.notebook_queries import (
    ExecutionNotebookQueryService,
    NotebookCellView,
)
from executor_service.domain.enums import RuntimeType
from executor_service.domain.errors import (
    ExecutionNotebookNotAvailableError,
    NotebookCellNotFoundError,
    NotebookReadError,
)
from executor_service.domain.runtime import RuntimeFileMetadata
from executor_service.interfaces.mcp.server import _notebook_cell_content
from executor_service.result_summaries import summarize_outputs


class ExecutionLookup:
    def __init__(self, execution: object) -> None:
        self.execution_value = execution

    async def execution(self, execution_id: UUID) -> object:
        del execution_id
        return self.execution_value


class NotebookStorage:
    def __init__(self, notebook: dict[str, Any] | Exception) -> None:
        self.notebook = notebook
        self.calls: list[tuple[RuntimeType, UUID | None, str]] = []

    async def read_notebook(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
    ) -> dict[str, Any]:
        self.calls.append((runtime_type, preferred_target_id, path))
        if isinstance(self.notebook, Exception):
            raise self.notebook
        return self.notebook

    async def write_notebook(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
        notebook: dict[str, Any],
    ) -> None:
        del runtime_type, preferred_target_id, path
        self.notebook = notebook

    async def write_text(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
        content: str,
    ) -> RuntimeFileMetadata:
        del runtime_type, preferred_target_id
        return RuntimeFileMetadata(
            path=path,
            name=path.rsplit("/", 1)[-1],
            size_bytes=len(content.encode()),
            modified_ns=0,
            media_type="text/plain",
            checksum_sha256="0" * 64,
        )


def _service(
    notebook: dict[str, Any] | Exception,
    *,
    notebook_path: str
    | None = "users/u/executions/e/notebooks/execution.ipynb",
) -> tuple[ExecutionNotebookQueryService, NotebookStorage, UUID, UUID]:
    execution_id = uuid4()
    target_id = uuid4()
    execution = SimpleNamespace(
        id=execution_id,
        runtime_type=RuntimeType.JUPYTER,
        runtime_target_id=target_id,
        notebook_path=notebook_path,
    )
    storage = NotebookStorage(notebook)
    return (
        ExecutionNotebookQueryService(
            cast(Any, ExecutionLookup(execution)), storage
        ),
        storage,
        execution_id,
        target_id,
    )


def _notebook() -> dict[str, Any]:
    return {
        "metadata": {"kernelspec": {"name": "basic"}},
        "cells": [
            {
                "id": "first",
                "cell_type": "code",
                "execution_count": 1,
                "source": ["value = 40\n", "print(value)"],
                "metadata": {},
                "outputs": [
                    {"output_type": "stream", "name": "stdout", "text": "40\n"}
                ],
            },
            {
                "id": "second",
                "cell_type": "markdown",
                "source": "# Result",
                "metadata": {},
            },
        ],
    }


async def test_notebook_read_uses_runtime_storage_and_paginates_cells() -> (
    None
):
    service, storage, execution_id, target_id = _service(_notebook())

    view = await service.read_notebook(
        execution_id, view="FULL", start_index=1, limit=1
    )

    assert view.total_count == 2
    assert view.start_index == 1
    assert [cell.source for cell in view.cells] == ["# Result"]
    assert storage.calls == [
        (
            RuntimeType.JUPYTER,
            target_id,
            "users/u/executions/e/notebooks/execution.ipynb",
        )
    ]


async def test_notebook_summary_omits_raw_outputs_but_full_preserves_them() -> (
    None
):
    service, _, execution_id, _ = _service(_notebook())

    summary = await service.read_notebook(
        execution_id, view="SUMMARY", start_index=0, limit=1
    )
    full = await service.read_notebook(
        execution_id, view="FULL", start_index=0, limit=1
    )

    assert summary.view == "SUMMARY"
    assert summary.cells[0].outputs == []
    assert summary.cells[0].output_summary.output_count == 1
    assert full.view == "FULL"
    assert full.cells[0].outputs == [
        {"output_type": "stream", "name": "stdout", "text": "40\n"}
    ]


async def test_notebook_cell_returns_full_source_and_outputs() -> None:
    service, _, execution_id, _ = _service(_notebook())

    cell = await service.read_cell(execution_id, 0)

    assert cell.source == "value = 40\nprint(value)"
    assert cell.outputs[0]["text"] == "40\n"


async def test_notebook_read_reports_unavailable_runtime_storage() -> None:
    service, _, execution_id, _ = _service(RuntimeError("offline"))

    with pytest.raises(ExecutionNotebookNotAvailableError):
        await service.read_notebook(execution_id)


async def test_notebook_read_requires_persisted_notebook_path() -> None:
    service, _, execution_id, _ = _service(_notebook(), notebook_path=None)

    with pytest.raises(ExecutionNotebookNotAvailableError):
        await service.read_notebook(execution_id)


async def test_notebook_indices_and_shape_are_validated() -> None:
    service, _, execution_id, _ = _service(_notebook())
    malformed, _, malformed_id, _ = _service(
        {"metadata": {}, "cells": "invalid"}
    )

    with pytest.raises(NotebookCellNotFoundError):
        await service.read_cell(execution_id, -1)
    with pytest.raises(NotebookCellNotFoundError):
        await service.read_cell(execution_id, 2)
    with pytest.raises(NotebookReadError):
        await service.read_notebook(execution_id, start_index=-1)
    with pytest.raises(NotebookReadError):
        await service.read_notebook(execution_id, limit=0)
    with pytest.raises(NotebookReadError):
        await malformed.read_notebook(malformed_id)


def test_mcp_notebook_cell_content_preserves_text_and_images() -> None:
    content = _notebook_cell_content(
        NotebookCellView(
            index=0,
            id="cell-0",
            type="code",
            execution_count=1,
            source="display(value)",
            line_count=1,
            metadata={},
            output_summary=summarize_outputs(
                [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": "ready\n",
                    },
                    {
                        "output_type": "display_data",
                        "data": {
                            "text/plain": "<Figure size 640x480>",
                            "image/png": "aW1hZ2UtYnl0ZXM=",
                        },
                        "metadata": {},
                    },
                ]
            ),
            outputs=[
                {"output_type": "stream", "name": "stdout", "text": "ready\n"},
                {
                    "output_type": "display_data",
                    "data": {
                        "text/plain": "<Figure size 640x480>",
                        "image/png": "aW1hZ2UtYnl0ZXM=",
                    },
                    "metadata": {},
                },
            ],
        )
    )

    texts = [item.text for item in content if isinstance(item, TextContent)]
    images = [item for item in content if isinstance(item, ImageContent)]
    assert "display(value)" in texts
    assert "ready\n" in texts
    assert "[text/plain]\n<Figure size 640x480>" in texts
    assert [(item.mime_type, item.data) for item in images] == [
        ("image/png", "aW1hZ2UtYnl0ZXM=")
    ]
