"""Read-only notebook transport contracts."""

from typing import Any
from uuid import UUID

from executor_service.application.notebook_queries import (
    NotebookCellView,
    NotebookResponseFormat,
    NotebookView,
)
from executor_service.interfaces._contracts.common import ContractModel
from executor_service.result_summaries import OutputSummary


class NotebookCellSummaryResponse(ContractModel):
    index: int
    id: str | None
    type: str
    execution_count: int | None
    source_preview: str
    source_truncated: bool
    line_count: int
    metadata: dict[str, Any]
    output_summary: OutputSummary

    @classmethod
    def from_view(
        cls, view: NotebookCellView
    ) -> "NotebookCellSummaryResponse":
        return cls(
            index=view.index,
            id=view.id,
            type=view.type,
            execution_count=view.execution_count,
            source_preview=view.source[:500],
            source_truncated=len(view.source) > 500,
            line_count=view.line_count,
            metadata=view.metadata,
            output_summary=view.output_summary,
        )


class NotebookCellResponse(ContractModel):
    index: int
    id: str | None
    type: str
    execution_count: int | None
    source: str
    line_count: int
    metadata: dict[str, Any]
    output_summary: OutputSummary
    outputs: list[dict[str, Any]]

    @classmethod
    def from_view(cls, view: NotebookCellView) -> "NotebookCellResponse":
        return cls(
            **{field: getattr(view, field) for field in cls.model_fields}
        )


class NotebookPage(ContractModel):
    start_index: int
    limit: int
    total_count: int
    has_more: bool


class ExecutionNotebookResponse(ContractModel):
    execution_id: UUID
    view: NotebookResponseFormat
    metadata: dict[str, Any]
    cells: list[NotebookCellSummaryResponse | NotebookCellResponse]
    page: NotebookPage

    @classmethod
    def from_view(cls, view: NotebookView) -> "ExecutionNotebookResponse":
        return cls(
            execution_id=view.execution_id,
            view=view.view,
            metadata=view.metadata,
            cells=[
                (
                    NotebookCellSummaryResponse.from_view(cell)
                    if view.view == "SUMMARY"
                    else NotebookCellResponse.from_view(cell)
                )
                for cell in view.cells
            ],
            page=NotebookPage(
                start_index=view.start_index,
                limit=view.limit,
                total_count=view.total_count,
                has_more=view.start_index + len(view.cells) < view.total_count,
            ),
        )


class ExecutionNotebookCellResponse(ContractModel):
    execution_id: UUID
    cell: NotebookCellResponse

    @classmethod
    def from_view(
        cls, execution_id: UUID, view: NotebookCellView
    ) -> "ExecutionNotebookCellResponse":
        return cls(
            execution_id=execution_id,
            cell=NotebookCellResponse.from_view(view),
        )
