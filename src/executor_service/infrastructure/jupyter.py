"""Jupyter implementation of the generic RuntimeDriver contract."""

import json
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
import websockets
from websockets.exceptions import WebSocketException
from websockets.typing import Subprotocol

from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeExecutionError,
    RuntimeExecutionResult,
    RuntimeFileMetadata,
    RuntimeFileState,
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
            if active_session_count is not None and not isinstance(active_session_count, int):
                raise TypeError("kernels must be an integer")
            return {"active_session_count": active_session_count}
        except (TypeError, ValueError) as exc:
            raise RuntimeDriverError("Jupyter status response is invalid.") from exc

    async def supported_profiles(self) -> list[str]:
        response = await self._request("GET", "/api/kernelspecs")
        return sorted(response.json().get("kernelspecs", {}).keys())

    async def resource_status(self) -> RuntimeResourceObservation:
        response = await self._request("GET", "/executor/resource-status")
        try:
            payload = response.json()
            if payload.get("schema_version") != "1.0":
                raise ValueError("unsupported resource schema version")
            observed_at = datetime.fromisoformat(str(payload["observed_at"]).replace("Z", "+00:00"))
            if observed_at.tzinfo is None:
                raise ValueError("observed_at must include a timezone")
            cpu = _resource_metric(
                payload["cpu"], used_key="used_cores", capacity_key="capacity_cores"
            )
            memory = _resource_metric(
                payload["memory"], used_key="used_bytes", capacity_key="capacity_bytes"
            )
            process_count = payload.get("process_count")
            if process_count is not None and not isinstance(process_count, int):
                raise TypeError("process_count must be an integer")
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeDriverError("Jupyter resource response is invalid.") from exc
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
            "POST", f"/api/kernels/{session_id}/interrupt", allowed_statuses={204, 404}
        )

    async def delete_session(self, session_id: str) -> None:
        await self._request("DELETE", f"/api/kernels/{session_id}", allowed_statuses={204, 404})

    async def session_exists(self, session_id: str) -> bool:
        response = await self._request(
            "GET", f"/api/kernels/{session_id}", allowed_statuses={200, 404}
        )
        return response.status_code == 200

    async def execute(self, session_id: str, code: str) -> RuntimeExecutionResult:
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
                        idle_received = content.get("execution_state") == "idle"
                    elif channel == "iopub":
                        output = _as_notebook_output(msg_type, content)
                        if output is not None:
                            outputs.append(output)
                            if output["output_type"] == "error":
                                error_message = _error_summary(output)
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise RuntimeDriverError("Jupyter kernel channel became unavailable.") from exc

        if error_message is not None:
            raise RuntimeExecutionError(error_message, outputs)
        return RuntimeExecutionResult(outputs=outputs, execution_count=execution_count)

    async def prepare_workspace(self, workspace_path: str) -> None:
        await self._request(
            "POST",
            "/executor/storage/workspaces/prepare",
            json={"workspace_path": workspace_path},
            timeout=self._storage_timeout,
        )

    async def artifact_snapshot(self, workspace_path: str) -> RuntimeStorageSnapshot:
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
            raise RuntimeDriverError("Jupyter Artifact snapshot response is invalid.") from exc
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
                    str(payload["media_type"]) if payload.get("media_type") is not None else None
                ),
                checksum_sha256=checksum,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeDriverError("Jupyter file metadata response is invalid.") from exc

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
            raise RuntimeDriverError("Jupyter manifest response is invalid.") from exc

    async def write_notebook(self, path: str, notebook: dict[str, Any]) -> None:
        await self._request(
            "PUT",
            f"/api/contents/{_contents_path(path)}",
            json={"type": "notebook", "format": "json", "content": notebook},
        )

    async def read_notebook(self, path: str) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"/api/contents/{_contents_path(path)}",
            params={"content": 1},
        )
        try:
            payload = response.json()
            content = payload.get("content")
            if payload.get("type") != "notebook" or not isinstance(content, dict):
                raise TypeError("content is not a notebook")
            return content
        except (TypeError, ValueError) as exc:
            raise RuntimeDriverError("Jupyter Notebook response is invalid.") from exc

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
            if allowed_statuses is None or response.status_code not in allowed_statuses:
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
    header, parent_header, metadata, content = (json.loads(part) for part in parts)
    return channel, {
        "header": header,
        "parent_header": parent_header,
        "metadata": metadata,
        "content": content,
    }


def _as_notebook_output(msg_type: str | None, content: dict[str, Any]) -> dict[str, Any] | None:
    if msg_type == "stream":
        return {"output_type": "stream", "name": content["name"], "text": content["text"]}
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


def _error_summary(content: dict[str, Any]) -> str:
    name = str(content.get("ename", "ExecutionError"))
    value = str(content.get("evalue", ""))
    return f"{name}: {value}"[:2000]


def _contents_path(path: str) -> str:
    pure = PurePosixPath(path)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise RuntimeDriverError("Runtime storage path must be a safe relative path.")
    return "/".join(quote(part, safe="") for part in pure.parts)


def _resource_metric(payload: object, *, used_key: str, capacity_key: str) -> RuntimeResourceMetric:
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
    if not isinstance(errors, list) or not all(isinstance(error, str) for error in errors):
        raise TypeError("errors must be a string array")
    return RuntimeResourceMetric(
        used=used,
        capacity=capacity,
        utilization=float(utilization) if utilization is not None else None,
        source=str(payload["source"]) if payload.get("source") is not None else None,
        estimated=payload.get("estimated") if isinstance(payload.get("estimated"), bool) else None,
        errors=tuple(errors),
    )
