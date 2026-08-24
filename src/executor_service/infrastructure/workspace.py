"""Runtime-owned workspace paths and Notebook document materialization."""

import re
from dataclasses import dataclass
from typing import Any

import nbformat

from executor_service.domain.models import Execution

SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


class WorkspacePathError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutionWorkspace:
    runtime_relative_path: str
    notebook_path: str
    reports_path: str
    artifacts_path: str
    manifest_path: str


class WorkspaceManager:
    """Build safe Runtime-relative paths without touching an Executor filesystem."""

    def plan(self, execution: Execution) -> ExecutionWorkspace:
        relative = "/".join(
            (
                "users",
                _segment(execution.user_id, "user_id"),
                "projects",
                _segment(execution.project_id or "unscoped", "project_id"),
                "sessions",
                _segment(execution.session_id or "unscoped", "session_id"),
                "executions",
                str(execution.id),
            )
        )
        return ExecutionWorkspace(
            runtime_relative_path=relative,
            notebook_path=f"{relative}/notebooks/execution.ipynb",
            reports_path=f"{relative}/reports",
            artifacts_path=f"{relative}/artifacts",
            manifest_path=f"{relative}/artifacts/manifest.jsonl",
        )

    def load_cells(self, execution: Execution) -> list[str]:
        cells = [
            step.code
            for step in sorted(execution.steps, key=lambda step: step.sequence)
        ]
        if not cells or any(not code.strip() for code in cells):
            raise WorkspacePathError(
                "ExecutionSpec contains no executable steps."
            )
        return cells

    def notebook_document(
        self,
        workspace: ExecutionWorkspace,
        runtime_profile: str,
        cells: list[str],
        outputs: list[list[dict[str, object]]],
        execution_counts: list[int | None],
    ) -> dict[str, Any]:
        notebook = nbformat.v4.new_notebook(
            metadata={
                "executor": {"workspace": workspace.runtime_relative_path},
                "kernelspec": {
                    "name": runtime_profile,
                    "display_name": runtime_profile,
                },
            }
        )
        notebook.cells = [
            nbformat.v4.new_code_cell(
                source=code,
                execution_count=execution_counts[index],
                outputs=[nbformat.from_dict(output) for output in output_list],
            )
            for index, (code, output_list) in enumerate(
                zip(cells, outputs, strict=True)
            )
        ]
        return notebook


def _segment(value: str, label: str) -> str:
    if not SAFE_SEGMENT.fullmatch(value):
        raise WorkspacePathError(
            f"{label} contains characters that are unsafe for a Runtime path."
        )
    return value
