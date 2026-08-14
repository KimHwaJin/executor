from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any

SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
ARTIFACT_DIRECTORIES = ("datasets", "plots", "models", "metrics", "reports", "logs", "other")


class StoragePathError(ValueError):
    pass


class RuntimeStorage:
    def __init__(self, root_dir: str | Path) -> None:
        self._root = Path(root_dir).resolve()

    def prepare_workspace(self, workspace_path: str) -> dict[str, str]:
        workspace = self._resolve(workspace_path)
        for relative in (
            "notebooks",
            "notebooks/.ipynb_checkpoints",
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
            "manifest_size": manifest.stat().st_size if manifest.is_file() else 0,
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
            raise StoragePathError("Path escapes the Jupyter root directory.") from exc
        return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
