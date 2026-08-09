"""Shared-PV path contract and notebook persistence."""

import re
from dataclasses import dataclass
from pathlib import Path

import nbformat

from executor_service.domain.enums import CodeSourceType
from executor_service.domain.models import Execution

SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
CELL_MARKER = re.compile(r"^\s*#\s*%%.*$", re.MULTILINE)


class WorkspacePathError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutionWorkspace:
    host_root: Path
    jupyter_relative_path: str
    code_dir: Path
    notebooks_dir: Path
    artifacts_dir: Path
    reports_dir: Path
    checkpoints_dir: Path
    notebook_file: Path


class WorkspaceManager:
    def __init__(self, host_root: Path) -> None:
        self._host_root = host_root.resolve()

    def prepare(self, execution: Execution) -> ExecutionWorkspace:
        relative = Path(
            "users",
            _segment(execution.requested_by_user_id, "user_id"),
            "projects",
            _segment(execution.project_id, "project_id"),
            "sessions",
            _segment(execution.session_id, "session_id"),
            "executions",
            str(execution.id),
        )
        root = (self._host_root / relative).resolve()
        _ensure_within(root, self._host_root)
        paths = {
            name: root / name
            for name in ("code", "notebooks", "artifacts", "reports", "checkpoints")
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)
        return ExecutionWorkspace(
            host_root=root,
            jupyter_relative_path=relative.as_posix(),
            code_dir=paths["code"],
            notebooks_dir=paths["notebooks"],
            artifacts_dir=paths["artifacts"],
            reports_dir=paths["reports"],
            checkpoints_dir=paths["checkpoints"],
            notebook_file=paths["notebooks"] / "execution.ipynb",
        )

    def load_cells(self, execution: Execution, workspace: ExecutionWorkspace) -> list[str]:
        if execution.code_source_type == CodeSourceType.INLINE:
            if execution.code is None:
                raise WorkspacePathError("INLINE execution has no code.")
            source = execution.code
            (workspace.code_dir / "execution.py").write_text(source, encoding="utf-8")
            return _split_python_cells(source)

        if execution.code_path is None:
            raise WorkspacePathError("PATH execution has no code path.")
        source_path = Path(execution.code_path)
        if not source_path.is_absolute():
            source_path = self._host_root / source_path
        source_path = source_path.resolve()
        _ensure_within(source_path, self._host_root)
        if not source_path.is_file():
            raise WorkspacePathError(f"Code source does not exist: {execution.code_path}")
        if source_path.suffix == ".ipynb":
            notebook = nbformat.read(source_path, as_version=4)
            cells = [cell.source for cell in notebook.cells if cell.cell_type == "code"]
        else:
            cells = _split_python_cells(source_path.read_text(encoding="utf-8"))
        if not cells:
            raise WorkspacePathError("Code source contains no executable cells.")
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
                "executor": {"workspace": workspace.jupyter_relative_path},
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


def _split_python_cells(source: str) -> list[str]:
    cells = [part.strip() for part in CELL_MARKER.split(source) if part.strip()]
    if not cells and source.strip():
        return [source.strip()]
    return cells
