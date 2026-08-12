"""Shared-PV path contract and notebook persistence."""

import re
from dataclasses import dataclass
from pathlib import Path

import nbformat

from executor_service.domain.models import Execution

SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")


class WorkspacePathError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutionWorkspace:
    host_root: Path
    runtime_relative_path: str
    code_dir: Path
    notebooks_dir: Path
    artifacts_dir: Path
    datasets_dir: Path
    plots_dir: Path
    models_dir: Path
    metrics_dir: Path
    reports_dir: Path
    logs_dir: Path
    other_dir: Path
    checkpoints_dir: Path
    notebook_file: Path


class WorkspaceManager:
    def __init__(self, host_root: Path) -> None:
        self._host_root = host_root.resolve()

    def prepare(self, execution: Execution) -> ExecutionWorkspace:
        relative = Path(
            "users",
            _segment(execution.user_id, "user_id"),
            "projects",
            _segment(execution.project_id, "project_id"),
            "sessions",
            _segment(execution.session_id, "session_id"),
            "executions",
            str(execution.id),
        )
        root = (self._host_root / relative).resolve()
        _ensure_within(root, self._host_root)
        paths = {name: root / name for name in ("code", "notebooks", "artifacts", "checkpoints")}
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        artifact_paths = {
            name: paths["artifacts"] / name
            for name in (
                "datasets",
                "plots",
                "models",
                "metrics",
                "reports",
                "logs",
                "other",
            )
        }
        for path in artifact_paths.values():
            path.mkdir(parents=True, exist_ok=True)
        workspace = ExecutionWorkspace(
            host_root=root,
            runtime_relative_path=relative.as_posix(),
            code_dir=paths["code"],
            notebooks_dir=paths["notebooks"],
            artifacts_dir=paths["artifacts"],
            datasets_dir=artifact_paths["datasets"],
            plots_dir=artifact_paths["plots"],
            models_dir=artifact_paths["models"],
            metrics_dir=artifact_paths["metrics"],
            reports_dir=artifact_paths["reports"],
            logs_dir=artifact_paths["logs"],
            other_dir=artifact_paths["other"],
            checkpoints_dir=paths["checkpoints"],
            notebook_file=paths["notebooks"] / "execution.ipynb",
        )
        (workspace.code_dir / "execution-spec.json").write_text(
            execution.source_content, encoding="utf-8"
        )
        return workspace

    def load_cells(self, execution: Execution, workspace: ExecutionWorkspace) -> list[str]:
        cells = [step.code for step in sorted(execution.steps, key=lambda step: step.sequence)]
        if not cells or any(not code.strip() for code in cells):
            raise WorkspacePathError("ExecutionSpec contains no executable steps.")
        return cells

    def write_notebook(
        self,
        workspace: ExecutionWorkspace,
        cells: list[str],
        outputs: list[list[dict[str, object]]],
        execution_counts: list[int | None],
    ) -> None:
        notebook = nbformat.v4.new_notebook(
            metadata={
                "executor": {"workspace": workspace.runtime_relative_path},
                "kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
            }
        )
        notebook.cells = [
            nbformat.v4.new_code_cell(
                source=code,
                execution_count=execution_counts[index],
                outputs=[nbformat.from_dict(output) for output in output_list],
            )
            for index, (code, output_list) in enumerate(zip(cells, outputs, strict=True))
        ]
        temporary = workspace.notebook_file.with_suffix(".ipynb.tmp")
        nbformat.write(notebook, temporary)
        temporary.replace(workspace.notebook_file)


def _segment(value: str, label: str) -> str:
    if not SAFE_SEGMENT.fullmatch(value):
        raise WorkspacePathError(f"{label} contains characters that are unsafe for a PV path.")
    return value


def _ensure_within(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise WorkspacePathError("Resolved path escapes the configured PV root.") from exc
