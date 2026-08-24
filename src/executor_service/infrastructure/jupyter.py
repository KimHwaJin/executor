"""Jupyter implementation of the generic RuntimeDriver contract."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
import websockets
from websockets.exceptions import WebSocketException
from websockets.typing import Subprotocol

from executor_service.domain.enums import RuntimeAbortStatus
from executor_service.domain.runtime import (
    RuntimeAbortResult,
    RuntimeDriverError,
    RuntimeExecutionError,
    RuntimeExecutionResult,
    RuntimeFileMetadata,
    RuntimeFileState,
    RuntimeNotebookPreparationResult,
    RuntimeNotebookSourceCell,
    RuntimeOutputHandler,
    RuntimeOutputRecord,
    RuntimeOutputRepresentation,
    RuntimeResourceMetric,
    RuntimeResourceObservation,
    RuntimeStorageSnapshot,
)


class JupyterRuntimeDriver:
    def __init__(
        self,
        endpoint: str,
        token: str,
        request_timeout_seconds: float = 30,
        storage_timeout_seconds: float = 300,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=self._endpoint,
            headers={"Authorization": f"token {token}"},
            timeout=request_timeout_seconds,
        )
        self._storage_timeout = storage_timeout_seconds

    async def close(self) -> None:
        await self._client.aclose()

    async def status(self) -> dict[str, Any]:
        response = await self._request("GET", "/api/status")
        try:
            payload = response.json()
            active_session_count = payload.get("kernels")
            if (
                type(active_session_count) is not int
                or active_session_count < 0
            ):
                raise TypeError("kernels must be a non-negative integer")
            return {"active_session_count": active_session_count}
        except (TypeError, ValueError) as exc:
            raise RuntimeDriverError(
                "Jupyter status response is invalid."
            ) from exc

    async def supported_profiles(self) -> list[str]:
        response = await self._request("GET", "/api/kernelspecs")
        return sorted(response.json().get("kernelspecs", {}).keys())

    async def resource_status(self) -> RuntimeResourceObservation:
        response = await self._request("GET", "/executor/resource-status")
        try:
            payload = response.json()
            if payload.get("schema_version") != "1.0":
                raise ValueError("unsupported resource schema version")
            observed_at = datetime.fromisoformat(
                str(payload["observed_at"]).replace("Z", "+00:00")
            )
            if observed_at.tzinfo is None:
                raise ValueError("observed_at must include a timezone")
            cpu = _resource_metric(
                payload["cpu"],
                used_key="used_cores",
                capacity_key="capacity_cores",
            )
            memory = _resource_metric(
                payload["memory"],
                used_key="used_bytes",
                capacity_key="capacity_bytes",
            )
            process_count = payload.get("process_count")
            if process_count is not None and not isinstance(
                process_count, int
            ):
                raise TypeError("process_count must be an integer")
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeDriverError(
                "Jupyter resource response is invalid."
            ) from exc
        return RuntimeResourceObservation(
            observed_at=observed_at,
            process_count=process_count,
            cpu=cpu,
            memory=memory,
        )

    async def start_session(self, profile: str, working_directory: str) -> str:
        response = await self._request(
            "POST",
            "/api/kernels",
            json={"name": profile, "path": working_directory},
        )
        return str(response.json()["id"])

    async def interrupt_session(self, session_id: str) -> None:
        await self._request(
            "POST",
            f"/api/kernels/{session_id}/interrupt",
            allowed_statuses={204, 404},
        )

    async def abort_session(
        self, session_id: str, timeout_seconds: float
    ) -> RuntimeAbortResult:
        try:
            interrupt = await self._request(
                "POST",
                f"/api/kernels/{session_id}/interrupt",
                allowed_statuses={204, 404},
            )
            if interrupt.status_code == 404:
                return RuntimeAbortResult(
                    RuntimeAbortStatus.SESSION_MISSING,
                    "Runtime session disappeared before interruption.",
                )
            async with asyncio.timeout(timeout_seconds):
                while True:
                    response = await self._request(
                        "GET",
                        f"/api/kernels/{session_id}",
                        allowed_statuses={200, 404},
                    )
                    if response.status_code == 404:
                        return RuntimeAbortResult(
                            RuntimeAbortStatus.SESSION_MISSING,
                            "Runtime session disappeared while confirming abort.",
                        )
                    try:
                        execution_state = response.json()["execution_state"]
                    except (KeyError, TypeError, ValueError) as exc:
                        raise RuntimeDriverError(
                            "Jupyter kernel status response is invalid."
                        ) from exc
                    if execution_state == "idle":
                        return RuntimeAbortResult(
                            RuntimeAbortStatus.IDLE_CONFIRMED
                        )
                    await asyncio.sleep(min(0.25, timeout_seconds / 10))
        except TimeoutError:
            return RuntimeAbortResult(
                RuntimeAbortStatus.FAILED,
                "Runtime did not confirm an idle session before the abort deadline.",
            )
        except RuntimeDriverError as exc:
            return RuntimeAbortResult(
                RuntimeAbortStatus.FAILED,
                str(exc)[:2000],
            )

    async def delete_session(self, session_id: str) -> None:
        await self._request(
            "DELETE", f"/api/kernels/{session_id}", allowed_statuses={204, 404}
        )

    async def session_exists(self, session_id: str) -> bool:
        response = await self._request(
            "GET", f"/api/kernels/{session_id}", allowed_statuses={200, 404}
        )
        return response.status_code == 200

    async def execute(
        self, session_id: str, code: str
    ) -> RuntimeExecutionResult:
        return await self._execute(session_id, code, output_handler=None)

    async def execute_streaming(
        self,
        session_id: str,
        code: str,
        output_handler: RuntimeOutputHandler,
    ) -> RuntimeExecutionResult:
        return await self._execute(
            session_id, code, output_handler=output_handler
        )

    async def _execute(
        self,
        session_id: str,
        code: str,
        *,
        output_handler: RuntimeOutputHandler | None,
    ) -> RuntimeExecutionResult:
        websocket_session_id = str(uuid4())
        message_id = str(uuid4())
        uri = self._channels_uri(session_id, websocket_session_id)
        request = {
            "header": {
                "msg_id": message_id,
                "username": "executor",
                "session": websocket_session_id,
                "date": datetime.now(UTC).isoformat(),
                "msg_type": "execute_request",
                "version": "5.3",
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code,
                "silent": False,
                "store_history": True,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
        }
        outputs: list[dict[str, Any]] = []
        execution_count: int | None = None
        reply_received = False
        idle_received = False
        error_message: str | None = None

        try:
            async with websockets.connect(
                uri,
                subprotocols=[Subprotocol("v1.kernel.websocket.jupyter.org")],
                additional_headers={"Authorization": f"token {self._token}"},
                max_size=None,
                ping_interval=20,
                ping_timeout=20,
            ) as websocket:
                await websocket.send(_serialize_v1("shell", request))
                while not (reply_received and idle_received):
                    raw = await websocket.recv()
                    channel, message = _deserialize_v1(raw)
                    parent_id = message.get("parent_header", {}).get("msg_id")
                    if parent_id != message_id:
                        continue
                    msg_type = message.get("header", {}).get("msg_type")
                    content = message.get("content", {})
                    if channel == "shell" and msg_type == "execute_reply":
                        reply_received = True
                        execution_count = content.get("execution_count")
                        if content.get("status") == "error":
                            error_message = _error_summary(content)
                    elif channel == "iopub" and msg_type == "status":
                        idle_received = (
                            content.get("execution_state") == "idle"
                        )
                    elif channel == "iopub":
                        output = _as_notebook_output(msg_type, content)
                        if output is not None:
                            if output_handler is None:
                                outputs.append(output)
                            else:
                                record = _as_output_record(msg_type, content)
                                if record is None:
                                    raise RuntimeDriverError(
                                        "Jupyter output mapping is incomplete."
                                    )
                                await output_handler(record)
                            if output["output_type"] == "error":
                                error_message = _error_summary(output)
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise RuntimeDriverError(
                "Jupyter kernel channel became unavailable."
            ) from exc

        if error_message is not None:
            raise RuntimeExecutionError(error_message, outputs)
        return RuntimeExecutionResult(
            outputs=outputs, execution_count=execution_count
        )

    async def prepare_notebook(
        self,
        workspace_path: str,
        execution_id: UUID,
        runtime_profile: str,
        cells: tuple[RuntimeNotebookSourceCell, ...],
    ) -> RuntimeNotebookPreparationResult:
        response = await self._request(
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
            timeout=self._storage_timeout,
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
        await self._request(
            "POST",
            "/executor/storage/workspaces/prepare",
            json={"workspace_path": workspace_path},
            timeout=self._storage_timeout,
        )

    async def artifact_snapshot(
        self, workspace_path: str
    ) -> RuntimeStorageSnapshot:
        response = await self._request(
            "POST",
            "/executor/storage/artifacts/snapshot",
            json={"workspace_path": workspace_path},
            timeout=self._storage_timeout,
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
        return RuntimeStorageSnapshot(files=files, manifest_size=manifest_size)

    async def file_metadata(self, path: str) -> RuntimeFileMetadata:
        response = await self._request(
            "POST",
            "/executor/storage/files/metadata",
            json={"path": path},
            timeout=self._storage_timeout,
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
        response = await self._request(
            "POST",
            "/executor/storage/manifests/read",
            json={"workspace_path": workspace_path, "start": start},
            timeout=self._storage_timeout,
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
        response = await self._request(
            "POST",
            "/executor/storage/notebooks/project",
            json={"notebook_path": path, "notebook": notebook},
            timeout=self._storage_timeout,
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
        response = await self._request(
            "GET",
            f"/api/contents/{_contents_path(path)}",
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
        await self._request(
            "PUT",
            f"/api/contents/{_contents_path(path)}",
            json={"type": "file", "format": "text", "content": content},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allowed_statuses: set[int] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
            if (
                allowed_statuses is None
                or response.status_code not in allowed_statuses
            ):
                response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            raise RuntimeDriverError(
                "Jupyter REST request failed: "
                f"method={method.upper()} path={path} status={exc.response.status_code}."
            ) from exc
        except httpx.RequestError as exc:
            raise RuntimeDriverError(
                "Jupyter REST request failed: "
                f"method={method.upper()} path={path} transport={type(exc).__name__}."
            ) from exc

    def _channels_uri(self, runtime_session_id: str, session_id: str) -> str:
        parsed = urlsplit(self._endpoint)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        base_path = parsed.path.rstrip("/")
        path = f"{base_path}/api/kernels/{runtime_session_id}/channels"
        query = urlencode({"session_id": session_id})
        return urlunsplit((scheme, parsed.netloc, path, query, ""))


def _serialize_v1(channel: str, message: dict[str, Any]) -> bytes:
    parts = [
        json.dumps(message[key], separators=(",", ":")).encode()
        for key in ("header", "parent_header", "metadata", "content")
    ]
    channel_bytes = channel.encode()
    offsets = [8 * (1 + 1 + len(parts) + 1)]
    offsets.append(offsets[-1] + len(channel_bytes))
    for part in parts:
        offsets.append(offsets[-1] + len(part))
    return b"".join(
        [
            len(offsets).to_bytes(8, "little"),
            *(offset.to_bytes(8, "little") for offset in offsets),
            channel_bytes,
            *parts,
        ]
    )


def _deserialize_v1(raw: str | bytes) -> tuple[str, dict[str, Any]]:
    if isinstance(raw, str):
        message = json.loads(raw)
        return str(message.get("channel", "")), message
    offset_count = int.from_bytes(raw[:8], "little")
    offsets = [
        int.from_bytes(raw[8 * (index + 1) : 8 * (index + 2)], "little")
        for index in range(offset_count)
    ]
    channel = raw[offsets[0] : offsets[1]].decode()
    parts = [raw[offsets[index] : offsets[index + 1]] for index in range(1, 5)]
    header, parent_header, metadata, content = (
        json.loads(part) for part in parts
    )
    return channel, {
        "header": header,
        "parent_header": parent_header,
        "metadata": metadata,
        "content": content,
    }


def _as_notebook_output(
    msg_type: str | None, content: dict[str, Any]
) -> dict[str, Any] | None:
    if msg_type == "stream":
        return {
            "output_type": "stream",
            "name": content["name"],
            "text": content["text"],
        }
    if msg_type in {"display_data", "execute_result"}:
        output = {
            "output_type": msg_type,
            "data": content.get("data", {}),
            "metadata": content.get("metadata", {}),
        }
        if msg_type == "execute_result":
            output["execution_count"] = content.get("execution_count")
        return output
    if msg_type == "error":
        return {
            "output_type": "error",
            "ename": content.get("ename", "Error"),
            "evalue": content.get("evalue", ""),
            "traceback": content.get("traceback", []),
        }
    return None


def _as_output_record(
    msg_type: str | None, content: dict[str, Any]
) -> RuntimeOutputRecord | None:
    if msg_type == "stream":
        return RuntimeOutputRecord(
            kind="STREAM",
            stream_name=str(content.get("name", "stdout")),
            representations=(
                RuntimeOutputRepresentation(
                    media_type="text/plain",
                    encoding="UTF8",
                    content=str(content.get("text", "")),
                ),
            ),
        )
    if msg_type in {"display_data", "execute_result"}:
        data = content.get("data", {})
        if not isinstance(data, dict):
            raise RuntimeDriverError("Jupyter display data is invalid.")
        representations = tuple(
            _output_representation(str(media_type), value)
            for media_type, value in data.items()
        )
        if not representations:
            representations = (
                RuntimeOutputRepresentation(
                    media_type="application/json",
                    encoding="UTF8",
                    content="{}",
                ),
            )
        metadata = content.get("metadata", {})
        if not isinstance(metadata, dict):
            raise RuntimeDriverError("Jupyter output metadata is invalid.")
        transient = content.get("transient")
        record_metadata = dict(metadata)
        if isinstance(transient, dict) and transient:
            record_metadata["transient"] = transient
        execution_count = content.get("execution_count")
        if execution_count is not None and type(execution_count) is not int:
            raise RuntimeDriverError("Jupyter execution_count is invalid.")
        return RuntimeOutputRecord(
            kind="RESULT" if msg_type == "execute_result" else "DISPLAY",
            execution_count=execution_count,
            representations=representations,
            metadata=record_metadata,
        )
    if msg_type == "error":
        name = str(content.get("ename", "Error"))
        value = str(content.get("evalue", ""))
        traceback = content.get("traceback", [])
        if not isinstance(traceback, list):
            raise RuntimeDriverError("Jupyter traceback is invalid.")
        text = "\n".join(str(line) for line in traceback)
        if not text:
            text = f"{name}: {value}"
        return RuntimeOutputRecord(
            kind="ERROR",
            representations=(
                RuntimeOutputRepresentation(
                    media_type="text/plain",
                    encoding="UTF8",
                    content=text,
                ),
            ),
            metadata={"ename": name, "evalue": value},
        )
    return None


def _output_representation(
    media_type: str, value: Any
) -> RuntimeOutputRepresentation:
    normalized_media_type = media_type.strip().lower()
    if not normalized_media_type or "/" not in normalized_media_type:
        raise RuntimeDriverError("Jupyter output media type is invalid.")
    base64_encoded = normalized_media_type == "application/pdf" or (
        normalized_media_type.startswith("image/")
        and normalized_media_type != "image/svg+xml"
    )
    if base64_encoded:
        if not isinstance(value, str):
            raise RuntimeDriverError(
                "Jupyter binary representation is invalid."
            )
        return RuntimeOutputRepresentation(
            media_type=normalized_media_type,
            encoding="BASE64",
            content=value,
        )
    if isinstance(value, str):
        content = value
    else:
        try:
            content = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeDriverError(
                "Jupyter output representation is not JSON serializable."
            ) from exc
    return RuntimeOutputRepresentation(
        media_type=normalized_media_type,
        encoding="UTF8",
        content=content,
    )


def _error_summary(content: dict[str, Any]) -> str:
    name = str(content.get("ename", "ExecutionError"))
    value = str(content.get("evalue", ""))
    return f"{name}: {value}"[:2000]


def _contents_path(path: str) -> str:
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


def _resource_metric(
    payload: object, *, used_key: str, capacity_key: str
) -> RuntimeResourceMetric:
    if not isinstance(payload, dict):
        raise TypeError("resource metric must be an object")
    used = payload.get(used_key)
    capacity = payload.get(capacity_key)
    utilization = payload.get("utilization")
    if used is not None and not isinstance(used, (int, float)):
        raise TypeError(f"{used_key} must be numeric")
    if capacity is not None and not isinstance(capacity, (int, float)):
        raise TypeError(f"{capacity_key} must be numeric")
    if utilization is not None and not isinstance(utilization, (int, float)):
        raise TypeError("utilization must be numeric")
    errors = payload.get("errors", [])
    if not isinstance(errors, list) or not all(
        isinstance(error, str) for error in errors
    ):
        raise TypeError("errors must be a string array")
    return RuntimeResourceMetric(
        used=used,
        capacity=capacity,
        utilization=float(utilization) if utilization is not None else None,
        source=str(payload["source"])
        if payload.get("source") is not None
        else None,
        estimated=payload.get("estimated")
        if isinstance(payload.get("estimated"), bool)
        else None,
        errors=tuple(errors),
    )
