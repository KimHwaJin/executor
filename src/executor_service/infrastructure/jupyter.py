"""Public Jupyter implementation of the RuntimeDriver contract."""

from contextlib import AbstractAsyncContextManager
from typing import Any
from uuid import UUID

import httpx

from executor_service.domain.runtime import (
    RuntimeAbortResult,
    RuntimeExecutionResult,
    RuntimeFileContent,
    RuntimeFileMetadata,
    RuntimeNotebookPreparationResult,
    RuntimeNotebookSourceCell,
    RuntimeOutputHandler,
    RuntimeResourceObservation,
    RuntimeStorageSnapshot,
)
from executor_service.infrastructure._jupyter import (
    JupyterHttpTransport,
    JupyterKernelExecutor,
    JupyterObservabilityClient,
    JupyterSessionClient,
    JupyterStorageClient,
    as_output_record,
    contents_path,
    deserialize_v1,
    serialize_v1,
)


class JupyterRuntimeDriver:
    def __init__(
        self,
        endpoint: str,
        token: str,
        request_timeout_seconds: float = 30,
        storage_timeout_seconds: float = 300,
        max_output_message_bytes: int = 33554432,
    ) -> None:
        self._transport = JupyterHttpTransport(
            endpoint,
            token,
            request_timeout_seconds,
            storage_timeout_seconds,
        )
        self._observability = JupyterObservabilityClient(self._transport)
        self._sessions = JupyterSessionClient(self._transport)
        self._execution = JupyterKernelExecutor(
            self._transport,
            max_output_message_bytes,
        )
        self._storage = JupyterStorageClient(self._transport)

    @property
    def _client(self) -> httpx.AsyncClient:
        """Compatibility seam for the existing transport-level tests."""

        return self._transport.client

    @_client.setter
    def _client(self, value: httpx.AsyncClient) -> None:
        self._transport.client = value

    async def close(self) -> None:
        await self._transport.close()

    async def status(self) -> dict[str, Any]:
        return await self._observability.status()

    async def supported_profiles(self) -> list[str]:
        return await self._observability.supported_profiles()

    async def resource_status(self) -> RuntimeResourceObservation:
        return await self._observability.resource_status()

    async def start_session(self, profile: str, working_directory: str) -> str:
        return await self._sessions.start(profile, working_directory)

    async def interrupt_session(self, session_id: str) -> None:
        await self._sessions.interrupt(session_id)

    async def abort_session(
        self, session_id: str, timeout_seconds: float
    ) -> RuntimeAbortResult:
        return await self._sessions.abort(session_id, timeout_seconds)

    async def delete_session(self, session_id: str) -> None:
        await self._sessions.delete(session_id)

    async def session_exists(self, session_id: str) -> bool:
        return await self._sessions.exists(session_id)

    async def execute(
        self, session_id: str, code: str
    ) -> RuntimeExecutionResult:
        return await self._execution.execute(session_id, code)

    async def execute_streaming(
        self,
        session_id: str,
        code: str,
        output_handler: RuntimeOutputHandler,
    ) -> RuntimeExecutionResult:
        return await self._execution.execute_streaming(
            session_id,
            code,
            output_handler,
        )

    async def prepare_notebook(
        self,
        workspace_path: str,
        execution_id: UUID,
        runtime_profile: str,
        cells: tuple[RuntimeNotebookSourceCell, ...],
    ) -> RuntimeNotebookPreparationResult:
        return await self._storage.prepare_notebook(
            workspace_path,
            execution_id,
            runtime_profile,
            cells,
        )

    async def prepare_workspace(self, workspace_path: str) -> None:
        await self._storage.prepare_workspace(workspace_path)

    async def artifact_snapshot(
        self, workspace_path: str
    ) -> RuntimeStorageSnapshot:
        return await self._storage.artifact_snapshot(workspace_path)

    async def file_metadata(self, path: str) -> RuntimeFileMetadata:
        return await self._storage.file_metadata(path)

    async def read_manifest(self, workspace_path: str, start: int) -> bytes:
        return await self._storage.read_manifest(workspace_path, start)

    async def write_notebook(
        self, path: str, notebook: dict[str, Any]
    ) -> None:
        await self._storage.write_notebook(path, notebook)

    async def read_notebook(self, path: str) -> dict[str, Any]:
        return await self._storage.read_notebook(path)

    async def write_text(self, path: str, content: str) -> None:
        await self._storage.write_text(path, content)

    def open_file(
        self, path: str, range_header: str | None
    ) -> AbstractAsyncContextManager[RuntimeFileContent]:
        return self._storage.open_file(path, range_header)


_as_output_record = as_output_record
_contents_path = contents_path
_deserialize_v1 = deserialize_v1
_serialize_v1 = serialize_v1

__all__ = ["JupyterRuntimeDriver"]
