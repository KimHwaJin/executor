"""Atomic filesystem implementation of shared execution result storage."""

import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from executor_service.domain.results import (
    RESULT_MANIFEST_SCHEMA_VERSION,
    ExecutionSourceReference,
    StepResultAppend,
    StepResultDescriptor,
    StepResultIdentity,
    StepResultReference,
)
from executor_service.domain.runtime import RuntimeOutputRecord

SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
TERMINAL_STATES = frozenset({"FINALIZED", "FAILED", "ABORTED"})
OUTPUT_KIND_NAMES = {
    "STREAM": "stream",
    "DISPLAY": "display_data",
    "RESULT": "execute_result",
    "ERROR": "error",
}
MEDIA_EXTENSIONS = {
    "application/json": ".json",
    "application/javascript": ".js",
    "application/pdf": ".pdf",
    "application/xml": ".xml",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
    "text/html": ".html",
    "text/markdown": ".md",
    "text/plain": ".txt",
}


class ResultStorageError(RuntimeError):
    """Shared result storage could not safely persist or read a result."""


class FilesystemExecutionResultStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def snapshot_source(
        self,
        execution_id: UUID,
        step_id: UUID,
        content: str,
    ) -> ExecutionSourceReference:
        return await asyncio.to_thread(
            self._snapshot_source, execution_id, step_id, content
        )

    def _snapshot_source(
        self,
        execution_id: UUID,
        step_id: UUID,
        content: str,
    ) -> ExecutionSourceReference:
        if not content.strip():
            raise ResultStorageError("Execution source must not be blank.")
        body = content.encode("utf-8")
        checksum = _sha256(body)
        relative = Path(
            "executions",
            str(execution_id),
            "sources",
            str(step_id),
            "source.py",
        )
        path = self._resolve_relative(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_bytes()
            if existing != body:
                raise ResultStorageError(
                    "Execution source snapshot conflicts with existing content."
                )
        else:
            _atomic_write(path, body)
        return ExecutionSourceReference(
            relative_path=relative.as_posix(),
            checksum_sha256=checksum,
            size_bytes=len(body),
        )

    async def read_source(self, reference: ExecutionSourceReference) -> str:
        return await asyncio.to_thread(self._read_source, reference)

    def _read_source(self, reference: ExecutionSourceReference) -> str:
        path = self._resolve_reference(reference.relative_path)
        body = path.read_bytes()
        if (
            len(body) != reference.size_bytes
            or _sha256(body) != reference.checksum_sha256
        ):
            raise ResultStorageError(
                "Execution source snapshot does not match its reference."
            )
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResultStorageError(
                "Execution source snapshot is not UTF-8."
            ) from exc

    async def begin_step_result(
        self,
        identity: StepResultIdentity,
        source: ExecutionSourceReference,
    ) -> None:
        async with await self._lock(identity):
            await asyncio.to_thread(self._begin_step_result, identity, source)

    def _begin_step_result(
        self,
        identity: StepResultIdentity,
        source: ExecutionSourceReference,
    ) -> None:
        final = self._result_directory(identity)
        partial = self._partial_directory(identity)
        if final.exists():
            descriptor = self._load_terminal_descriptor(final)
            if descriptor.source != source:
                raise ResultStorageError(
                    "Terminal Step result belongs to different source content."
                )
            return
        if partial.exists():
            state = self._load_state(partial)
            self._require_identity(state, identity)
            if state.get("source") != _source_json(source):
                raise ResultStorageError(
                    "Open Step result belongs to different source content."
                )
            return
        partial.mkdir(parents=True, exist_ok=False)
        source_body = self._read_source(source).encode("utf-8")
        _atomic_write(partial / "source.py", source_body)
        (partial / "outputs").mkdir()
        now = _utc_now()
        state = {
            "schema_version": RESULT_MANIFEST_SCHEMA_VERSION,
            "state": "OPEN",
            "identity": _identity_json(identity),
            "source": _source_json(source),
            "outputs": [],
            "batches": {},
            "output_count": 0,
            "representation_count": 0,
            "total_size_bytes": 0,
            "execution_count": None,
            "error_message": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        _atomic_json_write(partial / ".state.json", state)

    async def append_step_outputs(
        self,
        identity: StepResultIdentity,
        *,
        expected_offset: int,
        batch_id: UUID,
        records: tuple[RuntimeOutputRecord, ...],
    ) -> StepResultAppend:
        async with await self._lock(identity):
            return await asyncio.to_thread(
                self._append_step_outputs,
                identity,
                expected_offset,
                batch_id,
                records,
            )

    def _append_step_outputs(
        self,
        identity: StepResultIdentity,
        expected_offset: int,
        batch_id: UUID,
        records: tuple[RuntimeOutputRecord, ...],
    ) -> StepResultAppend:
        if not records:
            raise ResultStorageError("Step output batch must not be empty.")
        partial = self._partial_directory(identity, must_exist=True)
        state = self._load_state(partial)
        self._require_identity(state, identity)
        if state.get("state") != "OPEN":
            raise ResultStorageError(
                "Only an OPEN Step result accepts output."
            )
        request_digest = _records_digest(records)
        batch_key = str(batch_id)
        existing_batch = state["batches"].get(batch_key)
        if existing_batch is not None:
            if existing_batch.get("request_sha256") != request_digest:
                raise ResultStorageError(
                    "Step result batch ID conflicts with existing output."
                )
            return StepResultAppend(
                committed_offset=int(existing_batch["end_offset"]),
                output_count=int(existing_batch["output_count"]),
                representation_count=int(
                    existing_batch["representation_count"]
                ),
                total_size_bytes=int(existing_batch["total_size_bytes"]),
                replayed=True,
            )
        committed = int(state["output_count"])
        if expected_offset != committed:
            raise ResultStorageError(
                "Step result expected offset does not match committed output."
            )
        outputs: list[dict[str, Any]] = []
        representation_count = 0
        total_size_bytes = 0
        for index, record in enumerate(records):
            output = self._persist_record(
                partial,
                ordinal=expected_offset + index,
                record=record,
            )
            outputs.append(output)
            representation_count += len(output["representations"])
            total_size_bytes += sum(
                int(item["size_bytes"]) for item in output["representations"]
            )
        state["outputs"].extend(outputs)
        state["output_count"] = committed + len(outputs)
        state["representation_count"] = (
            int(state["representation_count"]) + representation_count
        )
        state["total_size_bytes"] = (
            int(state["total_size_bytes"]) + total_size_bytes
        )
        state["updated_at"] = _utc_now()
        state["batches"][batch_key] = {
            "request_sha256": request_digest,
            "start_offset": expected_offset,
            "end_offset": state["output_count"],
            "output_count": len(outputs),
            "representation_count": representation_count,
            "total_size_bytes": total_size_bytes,
        }
        _atomic_json_write(partial / ".state.json", state)
        return StepResultAppend(
            committed_offset=int(state["output_count"]),
            output_count=len(outputs),
            representation_count=representation_count,
            total_size_bytes=total_size_bytes,
            replayed=False,
        )

    async def finalize_step_result(
        self,
        identity: StepResultIdentity,
        *,
        execution_count: int | None,
        error_message: str | None = None,
    ) -> StepResultDescriptor:
        async with await self._lock(identity):
            return await asyncio.to_thread(
                self._seal,
                identity,
                "FAILED" if error_message else "FINALIZED",
                execution_count,
                error_message,
            )

    async def abort_step_result(
        self,
        identity: StepResultIdentity,
        *,
        reason: str,
    ) -> StepResultDescriptor:
        async with await self._lock(identity):
            return await asyncio.to_thread(
                self._seal, identity, "ABORTED", None, reason
            )

    def _seal(
        self,
        identity: StepResultIdentity,
        state_name: str,
        execution_count: int | None,
        error_message: str | None,
    ) -> StepResultDescriptor:
        if state_name not in TERMINAL_STATES:
            raise ResultStorageError("Unsupported terminal result state.")
        final = self._result_directory(identity)
        if final.exists():
            descriptor = self._load_terminal_descriptor(final)
            if descriptor.state != state_name:
                raise ResultStorageError(
                    "Terminal Step result state conflicts with replay."
                )
            return descriptor
        partial = self._partial_directory(identity, must_exist=True)
        state = self._load_state(partial)
        self._require_identity(state, identity)
        if state.get("state") != "OPEN":
            raise ResultStorageError("Step result is not OPEN.")
        state["state"] = state_name
        state["execution_count"] = execution_count
        state["error_message"] = (
            error_message[:2000] if error_message is not None else None
        )
        state["output_summary"] = _summary(state["outputs"])
        state["completed_at"] = _utc_now()
        state["updated_at"] = state["completed_at"]
        state.pop("batches", None)
        _atomic_json_write(partial / "manifest.json", state)
        (partial / ".state.json").unlink()
        _fsync_directory(partial)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(partial, final)
        _fsync_directory(final.parent)
        return self._load_terminal_descriptor(final)

    def _persist_record(
        self,
        partial: Path,
        *,
        ordinal: int,
        record: RuntimeOutputRecord,
    ) -> dict[str, Any]:
        kind = record.kind.upper()
        if kind not in OUTPUT_KIND_NAMES:
            raise ResultStorageError(f"Unsupported output kind: {kind!r}.")
        representations: list[dict[str, Any]] = []
        for index, representation in enumerate(record.representations):
            media_type = representation.media_type.lower().strip()
            body = _representation_body(
                representation.encoding, representation.content
            )
            suffix = _media_extension(media_type)
            filename = f"{ordinal:06d}-{kind.lower()}-{index:02d}{suffix}"
            path = partial / "outputs" / filename
            _atomic_write(path, body)
            representations.append(
                {
                    "media_type": media_type,
                    "encoding": representation.encoding,
                    "relative_path": f"outputs/{filename}",
                    "size_bytes": len(body),
                    "checksum_sha256": _sha256(body),
                    "complete": True,
                    "metadata": _json_value(representation.metadata),
                }
            )
        return {
            "ordinal": ordinal,
            "kind": kind,
            "stream_name": record.stream_name,
            "execution_count": record.execution_count,
            "representations": representations,
            "metadata": _json_value(record.metadata),
            "created_at": _utc_now(),
        }

    async def _lock(self, identity: StepResultIdentity) -> asyncio.Lock:
        key = self._result_relative(identity).as_posix()
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())

    def _load_terminal_descriptor(
        self, directory: Path
    ) -> StepResultDescriptor:
        manifest_path = directory / "manifest.json"
        body = manifest_path.read_bytes()
        try:
            value = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ResultStorageError(
                "Step result manifest is invalid JSON."
            ) from exc
        state = str(value.get("state", ""))
        if state not in TERMINAL_STATES:
            raise ResultStorageError("Step result manifest is not terminal.")
        identity = value.get("identity")
        source = value.get("source")
        if not isinstance(identity, dict) or not isinstance(source, dict):
            raise ResultStorageError(
                "Step result manifest identity is invalid."
            )
        relative = manifest_path.relative_to(self._root).as_posix()
        return StepResultDescriptor(
            state=state,
            reference=StepResultReference(
                relative_path=relative,
                checksum_sha256=_sha256(body),
                execution_attempt_id=UUID(
                    str(identity["execution_attempt_id"])
                ),
                fencing_token=int(identity["fencing_token"]),
            ),
            source=ExecutionSourceReference(
                relative_path=str(source["relative_path"]),
                checksum_sha256=str(source["checksum_sha256"]),
                size_bytes=int(source["size_bytes"]),
            ),
            output_count=int(value["output_count"]),
            representation_count=int(value["representation_count"]),
            total_size_bytes=int(value["total_size_bytes"]),
            output_summary=dict(value["output_summary"]),
            execution_count=(
                int(value["execution_count"])
                if value.get("execution_count") is not None
                else None
            ),
            error_message=(
                str(value["error_message"])
                if value.get("error_message") is not None
                else None
            ),
        )

    def _load_state(self, directory: Path) -> dict[str, Any]:
        try:
            value = json.loads((directory / ".state.json").read_bytes())
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ResultStorageError(
                "Open Step result state is unavailable."
            ) from exc
        if not isinstance(value, dict):
            raise ResultStorageError(
                "Open Step result state must be an object."
            )
        return value

    @staticmethod
    def _require_identity(
        state: dict[str, Any], identity: StepResultIdentity
    ) -> None:
        if state.get("identity") != _identity_json(identity):
            raise ResultStorageError("Step result identity conflicts.")

    def _result_relative(self, identity: StepResultIdentity) -> Path:
        return Path(
            "executions",
            str(identity.execution_id),
            "operations",
            str(identity.operation_id),
            "steps",
            str(identity.step_id),
            "attempts",
            str(identity.execution_attempt_id),
            str(identity.fencing_token),
        )

    def _result_directory(
        self, identity: StepResultIdentity, *, must_exist: bool = False
    ) -> Path:
        return self._resolve_relative(
            self._result_relative(identity), must_exist=must_exist
        )

    def _partial_directory(
        self, identity: StepResultIdentity, *, must_exist: bool = False
    ) -> Path:
        relative = self._result_relative(identity)
        partial = relative.with_name(f"{relative.name}.partial")
        return self._resolve_relative(partial, must_exist=must_exist)

    def _resolve_reference(self, raw: str) -> Path:
        candidate = Path(raw)
        if candidate.is_absolute():
            raise ResultStorageError(
                "Shared result reference must be relative."
            )
        return self._resolve_relative(candidate, must_exist=True)

    def _resolve_relative(
        self, relative: Path, *, must_exist: bool = False
    ) -> Path:
        if relative.is_absolute() or not relative.parts:
            raise ResultStorageError("Shared result path must be relative.")
        if any(
            part in {"", ".", ".."} or not SAFE_SEGMENT.fullmatch(part)
            for part in relative.parts
        ):
            raise ResultStorageError(
                "Shared result path contains an unsafe segment."
            )
        resolved = (self._root / relative).resolve(strict=must_exist)
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise ResultStorageError(
                "Shared result path escapes its root."
            ) from exc
        return resolved


def _identity_json(identity: StepResultIdentity) -> dict[str, object]:
    if identity.sequence < 0 or identity.fencing_token < 1:
        raise ResultStorageError("Step result identity values are invalid.")
    return {
        "execution_id": str(identity.execution_id),
        "operation_id": str(identity.operation_id),
        "step_id": str(identity.step_id),
        "sequence": identity.sequence,
        "execution_attempt_id": str(identity.execution_attempt_id),
        "fencing_token": identity.fencing_token,
    }


def _source_json(source: ExecutionSourceReference) -> dict[str, object]:
    return {
        "relative_path": source.relative_path,
        "checksum_sha256": source.checksum_sha256,
        "size_bytes": source.size_bytes,
    }


def _summary(outputs: list[dict[str, Any]]) -> dict[str, object]:
    output_types: Counter[str] = Counter()
    streams: set[str] = set()
    media_types: set[str] = set()
    image_count = 0
    has_error = False
    for output in outputs:
        kind = str(output["kind"])
        output_types[OUTPUT_KIND_NAMES[kind]] += 1
        has_error = has_error or kind == "ERROR"
        stream_name = output.get("stream_name")
        if kind == "STREAM" and isinstance(stream_name, str):
            streams.add(stream_name)
        for representation in output["representations"]:
            media_type = str(representation["media_type"])
            media_types.add(media_type)
            if media_type.startswith("image/"):
                image_count += 1
    return {
        "output_count": len(outputs),
        "output_types": dict(sorted(output_types.items())),
        "stream_names": sorted(streams),
        "mime_types": sorted(media_types),
        "has_image": image_count > 0,
        "image_count": image_count,
        "has_error": has_error,
    }


def _records_digest(records: tuple[RuntimeOutputRecord, ...]) -> str:
    return _sha256(
        json.dumps(
            [_record_json(record) for record in records],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def _record_json(record: RuntimeOutputRecord) -> dict[str, Any]:
    return {
        "kind": record.kind,
        "stream_name": record.stream_name,
        "execution_count": record.execution_count,
        "representations": [
            {
                "media_type": item.media_type,
                "encoding": item.encoding,
                "content": item.content,
                "metadata": _json_value(item.metadata),
            }
            for item in record.representations
        ],
        "metadata": _json_value(record.metadata),
    }


def _representation_body(encoding: str, content: str) -> bytes:
    try:
        if encoding == "UTF8":
            return content.encode("utf-8")
        if encoding == "BASE64":
            return base64.b64decode(content, validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ResultStorageError(
            "Output content does not match its declared encoding."
        ) from exc
    raise ResultStorageError(f"Unsupported output encoding: {encoding!r}.")


def _media_extension(media_type: str) -> str:
    if media_type in MEDIA_EXTENSIONS:
        return MEDIA_EXTENSIONS[media_type]
    if media_type.endswith("+json"):
        return ".json"
    if media_type.endswith("+xml"):
        return ".xml"
    if media_type.startswith("image/"):
        return ".img"
    if media_type.startswith("text/"):
        return ".txt"
    return ".bin"


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    _atomic_write(
        path,
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8"),
    )


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _json_value(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise ResultStorageError(
            "Result metadata must be JSON serializable."
        ) from exc


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def remove_partial_result(root: Path, identity: StepResultIdentity) -> None:
    """Maintenance helper; callers must first prove the fence is no longer active."""

    store = FilesystemExecutionResultStore(root)
    partial = store._partial_directory(identity)
    if partial.exists():
        shutil.rmtree(partial)
