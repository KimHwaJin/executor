import hashlib
import json
import mimetypes
from typing import Any, ClassVar

from executor_service.domain.runtime import (
    RuntimeFileMetadata,
    RuntimeFileState,
    RuntimeStorageSnapshot,
)


class InMemoryRuntimeStorage:
    """Runtime storage double that never touches the Executor filesystem."""

    files: ClassVar[dict[str, tuple[bytes, int]]] = {}
    notebooks: ClassVar[dict[str, dict[str, Any]]] = {}
    clock: ClassVar[int] = 0

    @classmethod
    def reset_storage(cls) -> None:
        cls.files = {}
        cls.notebooks = {}
        cls.clock = 0

    @classmethod
    def put_runtime_file(cls, path: str, content: bytes) -> None:
        cls.clock += 1
        cls.files[path] = (content, cls.clock)

    async def prepare_workspace(self, workspace_path: str) -> None:
        del workspace_path

    async def artifact_snapshot(
        self, workspace_path: str
    ) -> RuntimeStorageSnapshot:
        prefix = f"{workspace_path}/artifacts/"
        manifest = f"{prefix}manifest.jsonl"
        files = tuple(
            RuntimeFileState(
                path=path, size_bytes=len(content), modified_ns=modified
            )
            for path, (content, modified) in sorted(type(self).files.items())
            if path.startswith(prefix) and path != manifest
        )
        manifest_content = type(self).files.get(manifest, (b"", 0))[0]
        return RuntimeStorageSnapshot(
            files=files, manifest_size=len(manifest_content)
        )

    async def file_metadata(self, path: str) -> RuntimeFileMetadata:
        content, modified = type(self).files[path]
        return RuntimeFileMetadata(
            path=path,
            name=path.rsplit("/", 1)[-1],
            size_bytes=len(content),
            modified_ns=modified,
            media_type=mimetypes.guess_type(path)[0],
            checksum_sha256=hashlib.sha256(content).hexdigest(),
        )

    async def read_manifest(self, workspace_path: str, start: int) -> bytes:
        content = type(self).files.get(
            f"{workspace_path}/artifacts/manifest.jsonl", (b"", 0)
        )[0]
        return content[start:] if start <= len(content) else content

    async def write_notebook(
        self, path: str, notebook: dict[str, Any]
    ) -> None:
        type(self).notebooks[path] = notebook
        type(self).put_runtime_file(path, json.dumps(notebook).encode())

    async def read_notebook(self, path: str) -> dict[str, Any]:
        return type(self).notebooks[path]

    async def write_text(self, path: str, content: str) -> None:
        type(self).put_runtime_file(path, content.encode())
