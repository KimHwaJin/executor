"""Jupyter notebook, workspace, and artifact storage APIs."""

from contextlib import AbstractAsyncContextManager
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote
from uuid import UUID

from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeFileContent,
    RuntimeFileMetadata,
    RuntimeFileState,
    RuntimeNotebookPreparationResult,
    RuntimeNotebookSourceCell,
    RuntimeStorageSnapshot,
)
from executor_service.infrastructure._jupyter.transport import (
    JupyterHttpTransport,
)


class JupyterStorageClient:
    def __init__(self, transport: JupyterHttpTransport) -> None:
        self._transport = transport

    async def prepare_notebook(
        self,
        workspace_path: str,
        execution_id: UUID,
        runtime_profile: str,
        cells: tuple[RuntimeNotebookSourceCell, ...],
    ) -> RuntimeNotebookPreparationResult:
        response = await self._transport.request(
            "POST",
            "/executor/storage/notebooks/prepare",
            json={
                "workspace_path": workspace_path,
                "execution_id": str(execution_id),
                "runtime_profile": runtime_profile,
                "cells": [
                    {
                        "sequence": cell.sequence,
                        "operation_id": str(cell.operation_id),
                        "step_id": str(cell.step_id),
                        "source": cell.source,
                    }
                    for cell in cells
                ],
            },
            timeout=self._transport.storage_timeout_seconds,
        )
        try:
            payload = response.json()
            result = RuntimeNotebookPreparationResult(
                notebook_path=str(payload["notebook_path"]),
                prepared_cell_count=int(payload["prepared_cell_count"]),
                total_cell_count=int(payload["total_cell_count"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeDriverError(
                "Jupyter notebook preparation response is invalid."
            ) from exc
        if (
            result.notebook_path
            != f"{workspace_path}/notebooks/execution.ipynb"
            or result.prepared_cell_count != len(cells)
            or result.total_cell_count < result.prepared_cell_count
        ):
            raise RuntimeDriverError(
                "Jupyter notebook preparation acknowledgement is invalid."
            )
        return result

    async def prepare_workspace(self, workspace_path: str) -> None:
        await self._transport.request(
            "POST",
            "/executor/storage/workspaces/prepare",
            json={"workspace_path": workspace_path},
            timeout=self._transport.storage_timeout_seconds,
        )

    async def artifact_snapshot(
        self, workspace_path: str
    ) -> RuntimeStorageSnapshot:
        response = await self._transport.request(
            "POST",
            "/executor/storage/artifacts/snapshot",
            json={"workspace_path": workspace_path},
            timeout=self._transport.storage_timeout_seconds,
        )
        try:
            payload = response.json()
            files = tuple(
                RuntimeFileState(
                    path=str(item["path"]),
                    size_bytes=int(item["size_bytes"]),
                    modified_ns=int(item["modified_ns"]),
                )
                for item in payload["files"]
            )
            manifest_size = int(payload["manifest_size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeDriverError(
                "Jupyter Artifact snapshot response is invalid."
            ) from exc
        return RuntimeStorageSnapshot(
            files=files,
            manifest_size=manifest_size,
        )

    async def file_metadata(self, path: str) -> RuntimeFileMetadata:
        response = await self._transport.request(
            "POST",
            "/executor/storage/files/metadata",
            json={"path": path},
            timeout=self._transport.storage_timeout_seconds,
        )
        try:
            payload = response.json()
            checksum = str(payload["checksum_sha256"])
            if len(checksum) != 64:
                raise ValueError("invalid checksum")
            return RuntimeFileMetadata(
                path=str(payload["path"]),
                name=str(payload["name"]),
                size_bytes=int(payload["size_bytes"]),
                modified_ns=int(payload["modified_ns"]),
                media_type=(
                    str(payload["media_type"])
                    if payload.get("media_type") is not None
                    else None
                ),
                checksum_sha256=checksum,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeDriverError(
                "Jupyter file metadata response is invalid."
            ) from exc

    async def read_manifest(self, workspace_path: str, start: int) -> bytes:
        response = await self._transport.request(
            "POST",
            "/executor/storage/manifests/read",
            json={"workspace_path": workspace_path, "start": start},
            timeout=self._transport.storage_timeout_seconds,
        )
        try:
            payload = response.json()
            content = payload["content"]
            if not isinstance(content, str):
                raise TypeError("content must be text")
            return content.encode("utf-8")
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeDriverError(
                "Jupyter manifest response is invalid."
            ) from exc

    async def write_notebook(
        self, path: str, notebook: dict[str, Any]
    ) -> None:
        response = await self._transport.request(
            "POST",
            "/executor/storage/notebooks/project",
            json={"notebook_path": path, "notebook": notebook},
            timeout=self._transport.storage_timeout_seconds,
        )
        try:
            payload = response.json()
            if (
                payload.get("notebook_path") != path
                or int(payload["cell_count"]) != len(notebook["cells"])
                or len(str(payload["checksum_sha256"])) != 64
            ):
                raise ValueError("invalid notebook projection acknowledgement")
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeDriverError(
                "Jupyter notebook projection acknowledgement is invalid."
            ) from exc

    async def read_notebook(self, path: str) -> dict[str, Any]:
        response = await self._transport.request(
            "GET",
            f"/api/contents/{contents_path(path)}",
            params={"content": 1},
        )
        try:
            payload = response.json()
            content = payload.get("content")
            if payload.get("type") != "notebook" or not isinstance(
                content, dict
            ):
                raise TypeError("content is not a notebook")
            return content
        except (TypeError, ValueError) as exc:
            raise RuntimeDriverError(
                "Jupyter Notebook response is invalid."
            ) from exc

    async def write_text(self, path: str, content: str) -> None:
        await self._transport.request(
            "PUT",
            f"/api/contents/{contents_path(path)}",
            json={"type": "file", "format": "text", "content": content},
        )

    def open_file(
        self, path: str, range_header: str | None
    ) -> AbstractAsyncContextManager[RuntimeFileContent]:
        return self._transport.open_file(path, range_header)


def contents_path(path: str) -> str:
    pure = PurePosixPath(path)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise RuntimeDriverError(
            "Runtime storage path must be a safe relative path."
        )
    return "/".join(quote(part, safe="") for part in pure.parts)
