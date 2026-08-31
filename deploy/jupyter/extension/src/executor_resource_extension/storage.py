from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from typing import Any

SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
ARTIFACT_DIRECTORIES = (
    "datasets",
    "plots",
    "models",
    "metrics",
    "reports",
    "logs",
    "other",
)


class StoragePathError(ValueError):
    pass


class RuntimeStorage:
    def __init__(self, root_dir: str | Path) -> None:
        self._root = Path(root_dir).resolve()

    def prepare_workspace(self, workspace_path: str) -> dict[str, str]:
        workspace = self._resolve(workspace_path)
        for relative in (
            "notebooks",
            "reports",
            *(f"artifacts/{name}" for name in ARTIFACT_DIRECTORIES),
        ):
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        return {
            "workspace_path": workspace.relative_to(self._root).as_posix(),
            "notebook_path": (workspace / "notebooks/execution.ipynb")
            .relative_to(self._root)
            .as_posix(),
        }

    def snapshot(self, workspace_path: str) -> dict[str, Any]:
        workspace = self._resolve(workspace_path, must_exist=True)
        artifacts = self._ensure_within(workspace / "artifacts")
        manifest = artifacts / "manifest.jsonl"
        files = []
        if artifacts.is_dir():
            for path in sorted(artifacts.rglob("*")):
                if not path.is_file() or path == manifest:
                    continue
                stat = path.stat()
                files.append(
                    {
                        "path": path.relative_to(self._root).as_posix(),
                        "size_bytes": stat.st_size,
                        "modified_ns": stat.st_mtime_ns,
                    }
                )
        return {
            "files": files,
            "manifest_size": manifest.stat().st_size
            if manifest.is_file()
            else 0,
        }

    def prepare_notebook(
        self,
        *,
        workspace_path: str,
        execution_id: str,
        runtime_profile: str,
        cells: object,
    ) -> dict[str, Any]:
        workspace = self._resolve(workspace_path)
        if not isinstance(cells, list) or not cells:
            raise StoragePathError("Notebook cells must be a non-empty array.")
        notebook_cells: list[dict[str, Any]] = []
        for item in cells:
            if not isinstance(item, dict):
                raise StoragePathError("Notebook cell must be an object.")
            sequence = item.get("sequence")
            operation_id = item.get("operation_id")
            step_id = item.get("step_id")
            source = item.get("source")
            if (
                type(sequence) is not int
                or sequence < 0
                or not isinstance(operation_id, str)
                or not operation_id
                or not isinstance(step_id, str)
                or not step_id
                or not isinstance(source, str)
                or not source.strip()
            ):
                raise StoragePathError("Notebook cell contract is invalid.")
            notebook_cells.append(
                {
                    "cell_type": "code",
                    "execution_count": None,
                    "id": hashlib.sha256(
                        f"{execution_id}:{step_id}".encode()
                    ).hexdigest()[:16],
                    "metadata": {
                        "executor": {
                            "operation_id": operation_id,
                            "sequence": sequence,
                            "step_id": step_id,
                        }
                    },
                    "outputs": [],
                    "source": source,
                }
            )
        notebook_cells.sort(
            key=lambda value: value["metadata"]["executor"]["sequence"]
        )
        notebook = {
            "cells": notebook_cells,
            "metadata": {
                "executor": {"execution_id": execution_id},
                "kernelspec": {
                    "display_name": runtime_profile,
                    "language": "python",
                    "name": runtime_profile,
                },
                "language_info": {"name": "python"},
            },
            "nbformat": 4,
            "nbformat_minor": 5,
        }
        notebook_path = self._ensure_within(
            workspace / "notebooks/execution.ipynb"
        )
        notebook_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(notebook_path, notebook)
        return {
            "notebook_path": notebook_path.relative_to(self._root).as_posix(),
            "prepared_cell_count": len(notebook_cells),
            "total_cell_count": len(notebook_cells),
        }

    def project_notebook(
        self, *, notebook_path: str, notebook: object
    ) -> dict[str, Any]:
        path = self._resolve(notebook_path)
        if path.name != "execution.ipynb" or path.parent.name != "notebooks":
            raise StoragePathError(
                "Notebook projection path must target notebooks/execution.ipynb."
            )
        if not isinstance(notebook, dict):
            raise StoragePathError("Notebook projection must be an object.")
        cells = notebook.get("cells")
        if (
            not isinstance(cells, list)
            or notebook.get("nbformat") != 4
            or not isinstance(notebook.get("metadata"), dict)
        ):
            raise StoragePathError("Notebook projection contract is invalid.")
        _atomic_json_write(path, notebook)
        return {
            "notebook_path": path.relative_to(self._root).as_posix(),
            "cell_count": len(cells),
            "checksum_sha256": _sha256(path),
        }

    def file_metadata(self, raw_path: str) -> dict[str, Any]:
        path = self._resolve(raw_path, must_exist=True)
        if not path.is_file():
            raise StoragePathError("Path is not a file.")
        stat = path.stat()
        return {
            "path": path.relative_to(self._root).as_posix(),
            "name": path.name,
            "size_bytes": stat.st_size,
            "modified_ns": stat.st_mtime_ns,
            "media_type": mimetypes.guess_type(path.name)[0],
            "checksum_sha256": _sha256(path),
        }

    def resolve_file(self, raw_path: str) -> Path:
        path = self._resolve(raw_path, must_exist=True)
        if not path.is_file():
            raise StoragePathError("Path is not a file.")
        return path

    def read_manifest(self, workspace_path: str, start: int) -> dict[str, Any]:
        if start < 0:
            raise StoragePathError("Manifest offset must be non-negative.")
        workspace = self._resolve(workspace_path, must_exist=True)
        manifest = self._ensure_within(workspace / "artifacts/manifest.jsonl")
        if not manifest.is_file():
            return {"start": 0, "end": 0, "content": ""}
        size = manifest.stat().st_size
        effective_start = start if start <= size else 0
        with manifest.open("rb") as handle:
            handle.seek(effective_start)
            content = handle.read().decode("utf-8")
        return {"start": effective_start, "end": size, "content": content}

    def _resolve(self, raw_path: str, *, must_exist: bool = False) -> Path:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            path = candidate.resolve(strict=must_exist)
        else:
            if not candidate.parts or any(
                part in {"", ".", ".."} or not SAFE_SEGMENT.fullmatch(part)
                for part in candidate.parts
            ):
                raise StoragePathError("Path contains an unsafe segment.")
            path = (self._root / candidate).resolve(strict=must_exist)
        return self._ensure_within(path)

    def _ensure_within(self, path: Path) -> Path:
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise StoragePathError(
                "Path escapes the Jupyter root directory."
            ) from exc
        return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    """Publish notebook JSON atomically, readable by shared-volume viewers."""

    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
            if os.name == "posix":
                # NamedTemporaryFile starts at 0600. Set the final notebook
                # policy before publishing, including replacements of old
                # 0600 files. NTFS access remains governed by Windows ACLs.
                os.fchmod(handle.fileno(), 0o644)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
