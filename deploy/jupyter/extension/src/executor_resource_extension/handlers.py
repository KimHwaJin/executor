from __future__ import annotations

import asyncio
from typing import Any

from jupyter_server.base.handlers import (  # ty: ignore[unresolved-import]
    APIHandler,
)
from tornado import web  # ty: ignore[unresolved-import]

from executor_resource_extension.collector import ResourceCollector
from executor_resource_extension.file_download import (
    FileChangedError,
    FileRangeError,
    OpenedFileDownload,
    open_download,
)
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
        download: OpenedFileDownload | None = None
        try:
            path = await asyncio.to_thread(
                self.storage.resolve_file,
                self.get_query_argument("path"),
            )
            # Retired query bounds must not silently become full downloads.
            if (
                "start" in self.request.arguments
                or "end" in self.request.arguments
            ):
                raise web.HTTPError(400, reason="Use the Range header.")
            download = await open_download(
                path, self.request.headers.get("Range")
            )
            self.set_status(206 if download.partial else 200)
            self.set_header("Content-Type", "application/octet-stream")
            self.set_header("Content-Length", str(download.length))
            self.set_header("Cache-Control", "no-store")
            self.set_header("Accept-Ranges", "bytes")
            self.set_header("ETag", f'"{download.checksum_sha256}"')
            self.set_header("X-Checksum-SHA256", download.checksum_sha256)
            if download.partial:
                self.set_header(
                    "Content-Range",
                    f"bytes {download.start}-{download.end}/{download.size}",
                )
            while True:
                chunk = await asyncio.to_thread(download.read_chunk)
                if not chunk:
                    break
                self.write(chunk)
                await self.flush()
        except FileRangeError as exc:
            self.set_status(416)
            self.set_header("Accept-Ranges", "bytes")
            self.set_header("Content-Range", f"bytes */{exc.size}")
            self.finish({"message": str(exc)})
        except Exception as exc:
            if self._headers_written:
                # A JSON error or another file must never be appended to bytes
                # already sent as a download. Close the incomplete response.
                self.log.warning(
                    "Runtime file download interrupted: %s",
                    type(exc).__name__,
                )
                if self.request.connection is not None:
                    self.request.connection.close()
                return
            if isinstance(exc, FileChangedError):
                self.log.warning("Runtime file download setup failed: %s", exc)
                raise web.HTTPError(
                    409,
                    reason="File changed during download; retry after saving.",
                ) from exc
            self.write_storage_error(exc)
        finally:
            if download is not None:
                await asyncio.to_thread(download.close)


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
