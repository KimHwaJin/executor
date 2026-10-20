from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from jupyter_server.auth.decorator import (  # ty: ignore[unresolved-import]
    ws_authenticated,
)
from jupyter_server.base.handlers import (  # ty: ignore[unresolved-import]
    APIHandler,
    JupyterHandler,
)
from jupyter_server.base.websocket import (  # ty: ignore[unresolved-import]
    WebSocketMixin,
)
from tornado import web  # ty: ignore[unresolved-import]
from tornado.websocket import (  # ty: ignore[unresolved-import]
    WebSocketClosedError,
    WebSocketHandler,
)

from executor_resource_extension.collector import ResourceCollector
from executor_resource_extension.executions import (
    ExecutionJournals,
    JournalBusyError,
    JournalNotFoundError,
    KernelNotFoundError,
)
from executor_resource_extension.file_download import (
    FileChangedError,
    FileRangeError,
    OpenedFileDownload,
    open_download,
)
from executor_resource_extension.kernel_launcher import MEMORY_LIMIT_ENV
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


class KernelStartHandler(APIHandler):
    """Start a kernel with Execution-scoped environment and memory limit.

    The stock ``POST /api/kernels`` cannot pass ``env``; the kernel manager
    keeps these launch arguments, so automatic restarts reuse them.
    """

    @web.authenticated
    async def post(self) -> None:
        payload = self.get_json_body()
        if not isinstance(payload, dict):
            raise web.HTTPError(400, reason="JSON object body is required.")
        try:
            name = str(payload["name"])
            path = str(payload["path"])
            extra_env = payload.get("env") or {}
            memory_limit = payload.get("memory_limit_bytes")
            if not isinstance(extra_env, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in extra_env.items()
            ):
                raise ValueError("env must map strings to strings.")
            if memory_limit is not None and (
                not isinstance(memory_limit, int) or memory_limit <= 0
            ):
                raise ValueError("memory_limit_bytes must be positive.")
        except (KeyError, ValueError) as exc:
            raise web.HTTPError(
                422, reason="Invalid kernel start request."
            ) from exc
        env = {**os.environ, **extra_env}
        env.pop(MEMORY_LIMIT_ENV, None)
        if memory_limit is not None:
            env[MEMORY_LIMIT_ENV] = str(memory_limit)
        kernel_id = await self.kernel_manager.start_kernel(
            kernel_name=name, path=path, env=env
        )
        journals: ExecutionJournals = self.settings["executor_journals"]
        journals.protect(self.kernel_manager, kernel_id, extra_env.values())
        self.set_status(201)
        self.finish({"id": kernel_id})


class ExecutionStartHandler(APIHandler):
    @web.authenticated
    async def post(self, kernel_id: str) -> None:
        journals: ExecutionJournals = self.settings["executor_journals"]
        payload = self.get_json_body()
        if not isinstance(payload, dict):
            raise web.HTTPError(400, reason="JSON object body is required.")
        try:
            created = await journals.start(
                self.kernel_manager,
                kernel_id,
                str(payload["request_id"]),
                str(payload["code"]),
            )
        except (KeyError, ValueError) as exc:
            raise web.HTTPError(
                422, reason="request_id and code are required."
            ) from exc
        except KernelNotFoundError as exc:
            raise web.HTTPError(404, reason="Kernel was not found.") from exc
        self.set_status(201 if created else 200)
        self.finish({"created": created})


class ExecutionJournalHandler(APIHandler):
    @web.authenticated
    def delete(self, kernel_id: str, request_id: str) -> None:
        journals: ExecutionJournals = self.settings["executor_journals"]
        try:
            journals.delete(kernel_id, request_id)
        except ValueError as exc:
            raise web.HTTPError(422, reason="Invalid request_id.") from exc
        except JournalNotFoundError as exc:
            raise web.HTTPError(404, reason="Journal was not found.") from exc
        except JournalBusyError as exc:
            raise web.HTTPError(
                409, reason="Execution is still running."
            ) from exc
        self.set_status(204)
        self.finish()


class ExecutionStreamHandler(WebSocketMixin, WebSocketHandler, JupyterHandler):
    """Push journal entries after ?after=N; reconnecting loses nothing."""

    auth_resource = "kernels"

    def set_default_headers(self) -> None:
        """JupyterHandler's HTTP headers do not apply to WebSockets."""

    @ws_authenticated
    async def get(self, kernel_id: str, request_id: str) -> None:
        journals: ExecutionJournals = self.settings["executor_journals"]
        try:
            after = int(self.get_argument("after", "0"))
            self._entries = journals.follow(kernel_id, request_id, after)
            first = await anext(self._entries)
        except ValueError as exc:
            raise web.HTTPError(422, reason="Invalid stream request.") from exc
        except JournalNotFoundError as exc:
            raise web.HTTPError(404, reason="Journal was not found.") from exc
        self._first = first
        await super().get(kernel_id, request_id)

    def open(self, kernel_id: str, request_id: str) -> None:  # type: ignore[override]
        super().open()
        self._push = asyncio.create_task(self._send_entries())

    async def _send_entries(self) -> None:
        try:
            await self.write_message(json.dumps(self._first))
            async for entry in self._entries:
                await self.write_message(json.dumps(entry))
        except WebSocketClosedError:
            return
        finally:
            await self._entries.aclose()
        self.close()

    def on_close(self) -> None:
        self._push.cancel()

    def on_message(self, message: str | bytes) -> None:
        """The stream is one-way; client messages are ignored."""


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
