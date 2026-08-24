from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from executor_resource_extension.storage import SAFE_SEGMENT

JOURNAL_SCHEMA_VERSION = "1.0"
OUTPUT_KINDS = frozenset({"STREAM", "DISPLAY", "RESULT", "ERROR"})
CONTENT_ENCODINGS = frozenset({"UTF8", "BASE64"})
TERMINAL_STATES = frozenset({"FINALIZED", "ABORTED"})


class OutputJournalError(ValueError):
    pass


class OutputJournalConflictError(OutputJournalError):
    pass


class OutputJournalNotFoundError(OutputJournalError):
    pass


@dataclass(frozen=True, slots=True)
class JournalIdentity:
    workspace_path: str
    execution_id: str
    operation_id: str
    step_id: str
    sequence: int
    execution_attempt_id: str
    fencing_token: int
    runtime_target_id: str
    runtime_session_id: str

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> JournalIdentity:
        try:
            sequence = int(value["sequence"])
            fencing_token = int(value["fencing_token"])
            runtime_session_id = str(value["runtime_session_id"])
            workspace_path = str(value["workspace_path"])
            identifiers = {
                name: str(UUID(str(value[name])))
                for name in (
                    "execution_id",
                    "operation_id",
                    "step_id",
                    "execution_attempt_id",
                    "runtime_target_id",
                )
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise OutputJournalError(
                "Output Journal identity is invalid."
            ) from exc
        if sequence < 0:
            raise OutputJournalError("sequence must be non-negative.")
        if fencing_token < 1:
            raise OutputJournalError("fencing_token must be positive.")
        if not workspace_path:
            raise OutputJournalError("workspace_path is required.")
        if not 1 <= len(runtime_session_id) <= 1024:
            raise OutputJournalError(
                "runtime_session_id must contain 1 to 1024 characters."
            )
        return cls(
            workspace_path=workspace_path,
            execution_id=identifiers["execution_id"],
            operation_id=identifiers["operation_id"],
            step_id=identifiers["step_id"],
            sequence=sequence,
            execution_attempt_id=identifiers["execution_attempt_id"],
            fencing_token=fencing_token,
            runtime_target_id=identifiers["runtime_target_id"],
            runtime_session_id=runtime_session_id,
        )

    def storage_segments(self) -> tuple[str, ...]:
        return (
            self.operation_id,
            self.step_id,
            self.execution_attempt_id,
            str(self.fencing_token),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_path": self.workspace_path,
            "execution_id": self.execution_id,
            "operation_id": self.operation_id,
            "step_id": self.step_id,
            "sequence": self.sequence,
            "execution_attempt_id": self.execution_attempt_id,
            "fencing_token": self.fencing_token,
            "runtime_target_id": self.runtime_target_id,
            "runtime_session_id": self.runtime_session_id,
        }


class OutputJournalStorage:
    def __init__(self, root_dir: str | Path) -> None:
        self._root = Path(root_dir).resolve()
        self._locks = tuple(threading.Lock() for _ in range(64))

    def begin(self, identity: JournalIdentity) -> dict[str, Any]:
        directory = self._journal_directory(identity)
        lock = self._lock_for(directory)
        with lock:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "batches").mkdir(exist_ok=True)
            (directory / "content").mkdir(exist_ok=True)
            state_path = directory / "journal.json"
            if state_path.exists():
                state = self._load_state(directory)
                self._require_identity(state, identity)
                return self._journal_view(state)
            now = _utc_now()
            state = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "journal_id": str(uuid4()),
                "state": "OPEN",
                "identity": identity.as_dict(),
                "committed_offset": 0,
                "batch_count": 0,
                "output_count": 0,
                "representation_count": 0,
                "total_bytes": 0,
                "checksum_sha256": None,
                "abort_reason": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            _atomic_json_write(state_path, state)
            return self._journal_view(state)

    def append(
        self,
        identity: JournalIdentity,
        *,
        journal_id: str,
        expected_offset: int,
        batch_id: str,
        records: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if expected_offset < 0:
            raise OutputJournalError("expected_offset must be non-negative.")
        normalized_batch_id = _uuid_string(batch_id, "batch_id")
        if not isinstance(records, list) or not records:
            raise OutputJournalError("records must not be empty.")
        request_digest = _sha256_bytes(_canonical_json(records))
        directory = self._journal_directory(identity, must_exist=True)
        lock = self._lock_for(directory)
        with lock:
            state = self._load_and_repair(directory)
            self._require_journal(state, identity, journal_id)
            batch_path = directory / "batches" / f"{normalized_batch_id}.json"
            if batch_path.exists():
                batch = _read_json(batch_path)
                if batch.get("request_sha256") != request_digest:
                    raise OutputJournalConflictError(
                        "batch_id was already used with different records."
                    )
                return self._append_view(state, batch, replayed=True)
            if state["state"] != "OPEN":
                raise OutputJournalConflictError(
                    "Only an OPEN Output Journal accepts append."
                )
            if expected_offset != state["committed_offset"]:
                raise OutputJournalConflictError(
                    "expected_offset does not match committed_offset."
                )
            outputs = self._persist_records(
                directory,
                journal_id=journal_id,
                batch_id=normalized_batch_id,
                start_offset=expected_offset,
                records=records,
            )
            representation_count = sum(
                len(output["representations"]) for output in outputs
            )
            total_bytes = sum(
                representation["size_bytes"]
                for output in outputs
                for representation in output["representations"]
            )
            batch = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "batch_id": normalized_batch_id,
                "request_sha256": request_digest,
                "start_offset": expected_offset,
                "end_offset": expected_offset + len(outputs),
                "output_count": len(outputs),
                "representation_count": representation_count,
                "total_bytes": total_bytes,
                "outputs": outputs,
                "created_at": _utc_now(),
            }
            _atomic_json_write(batch_path, batch)
            state = self._repair_state(directory, state)
            return self._append_view(state, batch, replayed=False)

    def finalize(
        self, identity: JournalIdentity, *, journal_id: str
    ) -> dict[str, Any]:
        directory = self._journal_directory(identity, must_exist=True)
        lock = self._lock_for(directory)
        with lock:
            state = self._load_and_repair(directory)
            self._require_journal(state, identity, journal_id)
            if state["state"] == "FINALIZED":
                return self._journal_view(state)
            if state["state"] == "ABORTED":
                raise OutputJournalConflictError(
                    "An ABORTED Output Journal cannot be finalized."
                )
            state["state"] = "FINALIZED"
            state["checksum_sha256"] = self._journal_checksum(directory)
            now = _utc_now()
            state["updated_at"] = now
            state["completed_at"] = now
            _atomic_json_write(directory / "journal.json", state)
            return self._journal_view(state)

    def abort(
        self,
        identity: JournalIdentity,
        *,
        journal_id: str,
        reason: str,
    ) -> dict[str, Any]:
        normalized_reason = reason.strip()
        if not 1 <= len(normalized_reason) <= 2000:
            raise OutputJournalError(
                "abort reason must contain 1 to 2000 characters."
            )
        directory = self._journal_directory(identity, must_exist=True)
        lock = self._lock_for(directory)
        with lock:
            state = self._load_and_repair(directory)
            self._require_journal(state, identity, journal_id)
            if state["state"] == "ABORTED":
                return self._journal_view(state)
            if state["state"] == "FINALIZED":
                raise OutputJournalConflictError(
                    "A FINALIZED Output Journal cannot be aborted."
                )
            state["state"] = "ABORTED"
            state["abort_reason"] = normalized_reason
            now = _utc_now()
            state["updated_at"] = now
            state["completed_at"] = now
            _atomic_json_write(directory / "journal.json", state)
            return self._journal_view(state)

    def _persist_records(
        self,
        directory: Path,
        *,
        journal_id: str,
        batch_id: str,
        start_offset: int,
        records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise OutputJournalError(
                    "Each output record must be an object."
                )
            kind = str(record.get("kind", ""))
            if kind not in OUTPUT_KINDS:
                raise OutputJournalError(
                    f"Unsupported output record kind: {kind!r}."
                )
            raw_representations = record.get("representations")
            if (
                not isinstance(raw_representations, list)
                or not raw_representations
            ):
                raise OutputJournalError(
                    "Each output record requires representations."
                )
            ordinal = start_offset + index
            output_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"{journal_id}:{batch_id}:output:{index}",
                )
            )
            representations = [
                self._persist_representation(
                    directory,
                    journal_id=journal_id,
                    batch_id=batch_id,
                    output_id=output_id,
                    index=representation_index,
                    value=value,
                )
                for representation_index, value in enumerate(
                    raw_representations
                )
            ]
            metadata = record.get("metadata", {})
            if not isinstance(metadata, dict):
                raise OutputJournalError("record metadata must be an object.")
            stream_name = record.get("stream_name")
            if stream_name is not None:
                stream_name = str(stream_name)
            execution_count = record.get("execution_count")
            if execution_count is not None:
                try:
                    execution_count = int(execution_count)
                except (TypeError, ValueError) as exc:
                    raise OutputJournalError(
                        "execution_count must be an integer."
                    ) from exc
                if execution_count < 0:
                    raise OutputJournalError(
                        "execution_count must be non-negative."
                    )
            outputs.append(
                {
                    "output_id": output_id,
                    "ordinal": ordinal,
                    "kind": kind,
                    "stream_name": stream_name,
                    "execution_count": execution_count,
                    "representations": representations,
                    "metadata": _json_object(metadata, "record metadata"),
                    "created_at": _utc_now(),
                }
            )
        return outputs

    def _persist_representation(
        self,
        directory: Path,
        *,
        journal_id: str,
        batch_id: str,
        output_id: str,
        index: int,
        value: Any,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise OutputJournalError("Each representation must be an object.")
        media_type = str(value.get("media_type", "")).strip().lower()
        if not 1 <= len(media_type) <= 255 or "/" not in media_type:
            raise OutputJournalError("representation media_type is invalid.")
        encoding = str(value.get("encoding", ""))
        if encoding not in CONTENT_ENCODINGS:
            raise OutputJournalError(
                f"Unsupported representation encoding: {encoding!r}."
            )
        content = value.get("content")
        if not isinstance(content, str):
            raise OutputJournalError(
                "representation content must be a string."
            )
        try:
            body = (
                content.encode("utf-8")
                if encoding == "UTF8"
                else base64.b64decode(content, validate=True)
            )
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise OutputJournalError(
                "representation content does not match its encoding."
            ) from exc
        representation_id = str(
            uuid5(
                NAMESPACE_URL,
                f"{journal_id}:{batch_id}:{output_id}:representation:{index}",
            )
        )
        content_directory = directory / "content" / output_id
        content_directory.mkdir(parents=True, exist_ok=True)
        content_path = content_directory / f"{representation_id}.bin"
        _atomic_bytes_write(content_path, body)
        metadata = value.get("metadata", {})
        if not isinstance(metadata, dict):
            raise OutputJournalError(
                "representation metadata must be an object."
            )
        return {
            "representation_id": representation_id,
            "media_type": media_type,
            "size_bytes": len(body),
            "checksum_sha256": _sha256_bytes(body),
            "complete": True,
            "content_ref": (
                f"journal://{journal_id}/{output_id}/{representation_id}"
            ),
            "storage_path": content_path.relative_to(self._root).as_posix(),
            "metadata": _json_object(metadata, "representation metadata"),
        }

    def _load_and_repair(self, directory: Path) -> dict[str, Any]:
        return self._repair_state(directory, self._load_state(directory))

    def _repair_state(
        self, directory: Path, state: dict[str, Any]
    ) -> dict[str, Any]:
        batches = self._ordered_batches(directory)
        expected_offset = 0
        representation_count = 0
        total_bytes = 0
        for batch in batches:
            if batch["start_offset"] != expected_offset:
                raise OutputJournalConflictError(
                    "Output Journal batch offsets are not contiguous."
                )
            expected_offset = int(batch["end_offset"])
            representation_count += int(batch["representation_count"])
            total_bytes += int(batch["total_bytes"])
        repaired = {
            "committed_offset": expected_offset,
            "batch_count": len(batches),
            "output_count": expected_offset,
            "representation_count": representation_count,
            "total_bytes": total_bytes,
        }
        if any(state.get(key) != value for key, value in repaired.items()):
            if state["state"] in TERMINAL_STATES:
                raise OutputJournalConflictError(
                    "Terminal Output Journal metadata is inconsistent."
                )
            state.update(repaired)
            state["updated_at"] = _utc_now()
            _atomic_json_write(directory / "journal.json", state)
        return state

    def _ordered_batches(self, directory: Path) -> list[dict[str, Any]]:
        batches = [
            _read_json(path) for path in (directory / "batches").glob("*.json")
        ]
        return sorted(batches, key=lambda value: int(value["start_offset"]))

    def _journal_checksum(self, directory: Path) -> str:
        digest = hashlib.sha256()
        for batch in self._ordered_batches(directory):
            digest.update(_canonical_json(batch))
        return digest.hexdigest()

    def _journal_directory(
        self, identity: JournalIdentity, *, must_exist: bool = False
    ) -> Path:
        workspace = self._resolve_workspace(
            identity.workspace_path, must_exist=True
        )
        directory = workspace / "outputs"
        for segment in identity.storage_segments():
            if not SAFE_SEGMENT.fullmatch(segment):
                raise OutputJournalError(
                    "Output Journal identity contains an unsafe segment."
                )
            directory /= segment
        resolved = directory.resolve(strict=must_exist)
        self._ensure_within_root(resolved)
        return resolved

    def _resolve_workspace(self, raw_path: str, *, must_exist: bool) -> Path:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            raise OutputJournalError(
                "workspace_path must be relative to the Runtime root."
            )
        if not candidate.parts or any(
            part in {"", ".", ".."} or not SAFE_SEGMENT.fullmatch(part)
            for part in candidate.parts
        ):
            raise OutputJournalError(
                "workspace_path contains an unsafe segment."
            )
        workspace = (self._root / candidate).resolve(strict=must_exist)
        self._ensure_within_root(workspace)
        return workspace

    def _ensure_within_root(self, path: Path) -> None:
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise OutputJournalError(
                "Output Journal path escapes the Runtime root."
            ) from exc

    def _load_state(self, directory: Path) -> dict[str, Any]:
        state_path = directory / "journal.json"
        if not state_path.is_file():
            raise OutputJournalNotFoundError("Output Journal was not found.")
        state = _read_json(state_path)
        if state.get("schema_version") != JOURNAL_SCHEMA_VERSION:
            raise OutputJournalConflictError(
                "Output Journal schema_version is unsupported."
            )
        return state

    @staticmethod
    def _require_identity(
        state: dict[str, Any], identity: JournalIdentity
    ) -> None:
        if state.get("identity") != identity.as_dict():
            raise OutputJournalConflictError(
                "Output Journal identity does not match existing state."
            )

    def _require_journal(
        self,
        state: dict[str, Any],
        identity: JournalIdentity,
        journal_id: str,
    ) -> None:
        self._require_identity(state, identity)
        if state.get("journal_id") != _uuid_string(journal_id, "journal_id"):
            raise OutputJournalConflictError(
                "journal_id does not match Output Journal state."
            )

    def _lock_for(self, path: Path) -> threading.Lock:
        return self._locks[hash(path) % len(self._locks)]

    @staticmethod
    def _journal_view(state: dict[str, Any]) -> dict[str, Any]:
        return _json_object(state, "Output Journal state")

    @staticmethod
    def _append_view(
        state: dict[str, Any], batch: dict[str, Any], *, replayed: bool
    ) -> dict[str, Any]:
        return {
            "journal_id": state["journal_id"],
            "state": state["state"],
            "batch_id": batch["batch_id"],
            "start_offset": batch["start_offset"],
            "committed_offset": state["committed_offset"],
            "output_count": batch["output_count"],
            "representation_count": batch["representation_count"],
            "total_bytes": batch["total_bytes"],
            "outputs": [
                {
                    **output,
                    "representations": [
                        {
                            key: value
                            for key, value in representation.items()
                            if key != "storage_path"
                        }
                        for representation in output["representations"]
                    ],
                }
                for output in batch["outputs"]
            ],
            "replayed": replayed,
        }


def _uuid_string(value: str, name: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise OutputJournalError(f"{name} must be a UUID.") from exc


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OutputJournalError(
            "Output Journal value must be JSON serializable."
        ) from exc


def _json_object(value: Any, name: str) -> dict[str, Any]:
    normalized = json.loads(_canonical_json(value))
    if not isinstance(normalized, dict):
        raise OutputJournalError(f"{name} must be an object.")
    return normalized


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OutputJournalConflictError(
            "Output Journal storage contains invalid JSON."
        ) from exc
    if not isinstance(value, dict):
        raise OutputJournalConflictError(
            "Output Journal storage must contain JSON objects."
        )
    return value


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    _atomic_bytes_write(path, _canonical_json(value) + b"\n")


def _atomic_bytes_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
