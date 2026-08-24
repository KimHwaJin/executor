from __future__ import annotations

import asyncio
from typing import Any

from jupyter_server.base.handlers import (  # ty: ignore[unresolved-import]
    APIHandler,
)
from tornado import web  # ty: ignore[unresolved-import]

from executor_resource_extension.collector import ResourceCollector
from executor_resource_extension.output_journal import (
    JournalIdentity,
    OutputJournalConflictError,
    OutputJournalError,
    OutputJournalNotFoundError,
    OutputJournalStorage,
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

    @property
    def output_journals(self) -> OutputJournalStorage:
        return self.settings["executor_output_journals"]

    def payload(self) -> dict[str, Any]:
        payload = self.get_json_body()
        if not isinstance(payload, dict):
            raise web.HTTPError(400, reason="JSON object body is required.")
        return payload

    def write_storage_error(self, exc: Exception) -> None:
        if isinstance(exc, OutputJournalNotFoundError):
            raise web.HTTPError(
                404, reason="Output Journal was not found."
            ) from exc
        if isinstance(exc, OutputJournalConflictError):
            raise web.HTTPError(409, reason=str(exc)) from exc
        if isinstance(exc, OutputJournalError):
            raise web.HTTPError(422, reason=str(exc)) from exc
        if isinstance(
            exc, (StoragePathError, KeyError, TypeError, UnicodeDecodeError)
        ):
            raise web.HTTPError(422, reason=str(exc)) from exc
        if isinstance(exc, FileNotFoundError):
            raise web.HTTPError(
                404, reason="Runtime storage path was not found."
            ) from exc
        raise exc

    @staticmethod
    def journal_identity(payload: dict[str, Any]) -> JournalIdentity:
        value = payload.get("journal", payload)
        if not isinstance(value, dict):
            raise OutputJournalError("journal must be an object.")
        return JournalIdentity.from_mapping(value)


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
                self.output_journals.prepare_notebook,
                workspace_path=str(payload["workspace_path"]),
                execution_id=str(payload["execution_id"]),
                runtime_profile=str(payload["runtime_profile"]),
                cells=payload["cells"],
            )
        except Exception as exc:
            self.write_storage_error(exc)
            return
        self.finish(result)


class OutputJournalBeginHandler(StorageHandler):
    @web.authenticated
    async def post(self) -> None:
        try:
            payload = self.payload()
            identity = self.journal_identity(payload)
            result = await asyncio.to_thread(
                self.output_journals.begin,
                identity,
                str(payload["source"]),
            )
        except Exception as exc:
            self.write_storage_error(exc)
            return
        self.finish(result)


class OutputJournalAppendHandler(StorageHandler):
    @web.authenticated
    async def post(self) -> None:
        try:
            payload = self.payload()
            identity = self.journal_identity(payload)
            records = payload["records"]
            result = await asyncio.to_thread(
                self.output_journals.append,
                identity,
                journal_id=str(payload["journal_id"]),
                expected_offset=int(payload["expected_offset"]),
                batch_id=str(payload["batch_id"]),
                records=records,
            )
        except Exception as exc:
            self.write_storage_error(exc)
            return
        self.finish(result)


class OutputJournalFinalizeHandler(StorageHandler):
    @web.authenticated
    async def post(self) -> None:
        try:
            payload = self.payload()
            identity = self.journal_identity(payload)
            result = await asyncio.to_thread(
                self.output_journals.finalize,
                identity,
                journal_id=str(payload["journal_id"]),
            )
        except Exception as exc:
            self.write_storage_error(exc)
            return
        self.finish(result)


class OutputJournalAbortHandler(StorageHandler):
    @web.authenticated
    async def post(self) -> None:
        try:
            payload = self.payload()
            identity = self.journal_identity(payload)
            result = await asyncio.to_thread(
                self.output_journals.abort,
                identity,
                journal_id=str(payload["journal_id"]),
                reason=str(payload["reason"]),
            )
        except Exception as exc:
            self.write_storage_error(exc)
            return
        self.finish(result)


class OutputJournalMaterializeNotebookHandler(StorageHandler):
    @web.authenticated
    async def post(self) -> None:
        try:
            payload = self.payload()
            result = await asyncio.to_thread(
                self.output_journals.materialize_notebook,
                workspace_path=str(payload["workspace_path"]),
                runtime_profile=str(payload["runtime_profile"]),
                cells=payload["cells"],
            )
        except Exception as exc:
            self.write_storage_error(exc)
            return
        self.finish(result)
