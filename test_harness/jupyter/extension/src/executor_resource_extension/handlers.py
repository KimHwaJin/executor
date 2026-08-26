from __future__ import annotations

import asyncio
from typing import Any, BinaryIO, cast

from jupyter_server.base.handlers import (  # ty: ignore[unresolved-import]
    APIHandler,
)
from tornado import web  # ty: ignore[unresolved-import]

from executor_resource_extension.collector import ResourceCollector
from executor_resource_extension.storage import (
    RuntimeStorage,
    StoragePathError,
)


class ResourceStatusHandler(APIHandler):
    @web.authenticated
    def get(self) -> None:
        collector: ResourceCollector = self.settings[
            "executor_resource_collector"
        ]
        self.set_header("Cache-Control", "no-store")
        self.finish(collector.collect())

    def write_error(self, status_code: int, **kwargs: Any) -> None:
        self.set_header("Content-Type", "application/json")
        self.finish(
            {
                "status": status_code,
                "message": "Resource status collection failed.",
            }
        )


class StorageHandler(APIHandler):
    @property
    def storage(self) -> RuntimeStorage:
        return self.settings["executor_runtime_storage"]

    def payload(self) -> dict[str, Any]:
        payload = self.get_json_body()
        if not isinstance(payload, dict):
            raise web.HTTPError(400, reason="JSON object body is required.")
        return payload

    def write_storage_error(self, exc: Exception) -> None:
        if isinstance(
            exc, (StoragePathError, KeyError, TypeError, UnicodeDecodeError)
        ):
            raise web.HTTPError(422, reason=str(exc)) from exc
        if isinstance(exc, FileNotFoundError):
            raise web.HTTPError(
                404, reason="Runtime storage path was not found."
            ) from exc
        raise exc


class WorkspacePrepareHandler(StorageHandler):
    @web.authenticated
    async def post(self) -> None:
        try:
            result = await asyncio.to_thread(
                self.storage.prepare_workspace,
                str(self.payload()["workspace_path"]),
            )
        except Exception as exc:
            self.write_storage_error(exc)
            return
        self.finish(result)


class ArtifactSnapshotHandler(StorageHandler):
    @web.authenticated
    async def post(self) -> None:
        try:
            result = await asyncio.to_thread(
                self.storage.snapshot, str(self.payload()["workspace_path"])
            )
        except Exception as exc:
            self.write_storage_error(exc)
            return
        self.finish(result)


class FileMetadataHandler(StorageHandler):
    @web.authenticated
    async def post(self) -> None:
        try:
            result = await asyncio.to_thread(
                self.storage.file_metadata, str(self.payload()["path"])
            )
        except Exception as exc:
            self.write_storage_error(exc)
            return
        self.finish(result)


class FileContentHandler(StorageHandler):
    @web.authenticated
    async def get(self) -> None:
        handle: BinaryIO | None = None
        try:
            path = await asyncio.to_thread(
                self.storage.resolve_file,
                self.get_query_argument("path"),
            )
            size = path.stat().st_size
            start = int(self.get_query_argument("start", "0"))
            end = int(self.get_query_argument("end", str(size - 1)))
            if start < 0 or end < start or end >= size:
                raise StoragePathError("Requested file range is invalid.")
            self.set_header("Content-Type", "application/octet-stream")
            self.set_header("Content-Length", str(end - start + 1))
            self.set_header("Cache-Control", "no-store")
            opened = await asyncio.to_thread(_open_binary, path)
            handle = opened
            await asyncio.to_thread(opened.seek, start)
            remaining = end - start + 1
            while remaining:
                chunk = await asyncio.to_thread(
                    _read_chunk, opened, min(1024 * 1024, remaining)
                )
                if not chunk:
                    raise StoragePathError(
                        "Runtime file ended before the requested range."
                    )
                self.write(chunk)
                await self.flush()
                remaining -= len(chunk)
        except Exception as exc:
            self.write_storage_error(exc)
        finally:
            if handle is not None:
                await asyncio.to_thread(handle.close)


def _read_chunk(handle: BinaryIO, size: int) -> bytes:
    return handle.read(size)


def _open_binary(path: Any) -> BinaryIO:
    return cast(BinaryIO, path.open("rb"))


class ManifestReadHandler(StorageHandler):
    @web.authenticated
    async def post(self) -> None:
        try:
            payload = self.payload()
            result = await asyncio.to_thread(
                self.storage.read_manifest,
                str(payload["workspace_path"]),
                int(payload.get("start", 0)),
            )
        except Exception as exc:
            self.write_storage_error(exc)
            return
        self.finish(result)


class NotebookPrepareHandler(StorageHandler):
    @web.authenticated
    async def post(self) -> None:
        try:
            payload = self.payload()
            result = await asyncio.to_thread(
                self.storage.prepare_notebook,
                workspace_path=str(payload["workspace_path"]),
                execution_id=str(payload["execution_id"]),
                runtime_profile=str(payload["runtime_profile"]),
                cells=payload["cells"],
            )
        except Exception as exc:
            self.write_storage_error(exc)
            return
        self.finish(result)


class NotebookProjectHandler(StorageHandler):
    @web.authenticated
    async def post(self) -> None:
        try:
            payload = self.payload()
            result = await asyncio.to_thread(
                self.storage.project_notebook,
                notebook_path=str(payload["notebook_path"]),
                notebook=payload["notebook"],
            )
        except Exception as exc:
            self.write_storage_error(exc)
            return
        self.finish(result)
