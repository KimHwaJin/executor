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
JOURNAL_FILE_NAME = "journal.jsonl"
HEADER_RECORD = "HEADER"
BATCH_RECORD = "BATCH"
TERMINAL_RECORD = "TERMINAL"
IMAGE_EXTENSIONS = {
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
}


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

    def begin(self, identity: JournalIdentity, source: str) -> dict[str, Any]:
        if not isinstance(source, str) or not source.strip():
            raise OutputJournalError("source must not be blank.")
        source_body = source.encode("utf-8")
        source_checksum = _sha256_bytes(source_body)
        directory = self._journal_directory(identity)
        lock = self._lock_for(directory)
        with lock:
            directory.mkdir(parents=True, exist_ok=True)
            journal_path = directory / JOURNAL_FILE_NAME
            if journal_path.exists():
                state, _, header = self._load_journal(directory)
                self._require_identity(state, identity)
                if (
                    state.get("source_size_bytes") != len(source_body)
                    or state.get("source_checksum_sha256") != source_checksum
                    or header.get("source") != source
                ):
                    raise OutputJournalConflictError(
                        "Output Journal source does not match existing state."
                    )
                return self._journal_view(state)
            now = _utc_now()
            header = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "record_type": HEADER_RECORD,
                "journal_id": str(uuid4()),
                "identity": identity.as_dict(),
                "source": source,
                "source_size_bytes": len(source_body),
                "source_checksum_sha256": source_checksum,
                "created_at": now,
            }
            _atomic_bytes_write(journal_path, _canonical_json(header) + b"\n")
            state = self._state_from_entries(header, [], None)
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
            state, batches, _ = self._load_journal(directory)
            self._require_journal(state, identity, journal_id)
            batch = next(
                (
                    value
                    for value in batches
                    if value["batch_id"] == normalized_batch_id
                ),
                None,
            )
            if batch is not None:
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
                "record_type": BATCH_RECORD,
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
            _append_json_line(directory / JOURNAL_FILE_NAME, batch)
            state, _, _ = self._load_journal(directory)
            return self._append_view(state, batch, replayed=False)

    def finalize(
        self, identity: JournalIdentity, *, journal_id: str
    ) -> dict[str, Any]:
        directory = self._journal_directory(identity, must_exist=True)
        lock = self._lock_for(directory)
        with lock:
            state, batches, header = self._load_journal(directory)
            self._require_journal(state, identity, journal_id)
            if state["state"] == "FINALIZED":
                return self._journal_view(state)
            if state["state"] == "ABORTED":
                raise OutputJournalConflictError(
                    "An ABORTED Output Journal cannot be finalized."
                )
            now = _utc_now()
            terminal = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "record_type": TERMINAL_RECORD,
                "state": "FINALIZED",
                "checksum_sha256": self._journal_checksum(header, batches),
                "abort_reason": None,
                "created_at": now,
            }
            _append_json_line(directory / JOURNAL_FILE_NAME, terminal)
            state, _, _ = self._load_journal(directory)
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
            state, batches, header = self._load_journal(directory)
            self._require_journal(state, identity, journal_id)
            if state["state"] == "ABORTED":
                return self._journal_view(state)
            if state["state"] == "FINALIZED":
                raise OutputJournalConflictError(
                    "A FINALIZED Output Journal cannot be aborted."
                )
            now = _utc_now()
            terminal = {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "record_type": TERMINAL_RECORD,
                "state": "ABORTED",
                "checksum_sha256": self._journal_checksum(header, batches),
                "abort_reason": normalized_reason,
                "created_at": now,
            }
            _append_json_line(directory / JOURNAL_FILE_NAME, terminal)
            state, _, _ = self._load_journal(directory)
            return self._journal_view(state)

    def prepare_notebook(
        self,
        *,
        workspace_path: str,
        execution_id: str,
        runtime_profile: str,
        cells: list[dict[str, Any]],
    ) -> dict[str, Any]:
        normalized_execution_id = _uuid_string(execution_id, "execution_id")
        profile = _runtime_profile(runtime_profile)
        if not isinstance(cells, list) or not cells:
            raise OutputJournalError("cells must not be empty.")
        workspace = self._resolve_workspace(workspace_path, must_exist=True)
        notebook_path = workspace / "notebooks" / "execution.ipynb"
        prepared: list[dict[str, Any]] = []
        requested_sequences: set[int] = set()
        requested_steps: set[str] = set()
        for value in cells:
            if not isinstance(value, dict):
                raise OutputJournalError(
                    "Each prepared notebook cell must be an object."
                )
            try:
                sequence = int(value["sequence"])
                operation_id = _uuid_string(
                    value["operation_id"], "operation_id"
                )
                step_id = _uuid_string(value["step_id"], "step_id")
                source = value["source"]
            except (KeyError, TypeError, ValueError) as exc:
                raise OutputJournalError(
                    "Notebook preparation input is invalid."
                ) from exc
            if sequence < 0 or sequence in requested_sequences:
                raise OutputJournalError(
                    "Prepared cell sequences must be unique and non-negative."
                )
            if step_id in requested_steps:
                raise OutputJournalError(
                    "Prepared cell step IDs must be unique."
                )
            if not isinstance(source, str) or not source.strip():
                raise OutputJournalError(
                    "Prepared notebook cell source must not be blank."
                )
            prepared.append(
                {
                    "cell_id": _notebook_cell_id(
                        normalized_execution_id, step_id
                    ),
                    "execution_id": normalized_execution_id,
                    "operation_id": operation_id,
                    "step_id": step_id,
                    "sequence": sequence,
                    "source": source,
                }
            )
            requested_sequences.add(sequence)
            requested_steps.add(step_id)

        notebook_path.parent.mkdir(parents=True, exist_ok=True)
        lock = self._lock_for(notebook_path)
        with lock:
            notebook = _load_or_create_notebook(
                notebook_path,
                workspace_path=workspace_path,
                execution_id=normalized_execution_id,
                runtime_profile=profile,
            )
            notebook_cells = notebook["cells"]
            existing_by_id = {
                str(cell.get("id")): cell for cell in notebook_cells
            }
            existing_sequences = {
                int(metadata["sequence"]): str(cell.get("id"))
                for cell in notebook_cells
                if isinstance(cell, dict)
                and (metadata := _executor_cell_metadata(cell)) is not None
                and type(metadata.get("sequence")) is int
            }
            for value in prepared:
                cell_id = value["cell_id"]
                conflicting_id = existing_sequences.get(value["sequence"])
                if conflicting_id is not None and conflicting_id != cell_id:
                    raise OutputJournalConflictError(
                        "Notebook sequence belongs to another Step."
                    )
                existing = existing_by_id.get(cell_id)
                if existing is None:
                    notebook_cells.append(_prepared_notebook_cell(value))
                    continue
                _require_prepared_notebook_cell(existing, value)
            notebook_cells.sort(key=_notebook_cell_sort_key)
            _atomic_json_write(notebook_path, notebook)
        return {
            "notebook_path": notebook_path.relative_to(self._root).as_posix(),
            "prepared_cell_count": len(prepared),
            "total_cell_count": len(notebook["cells"]),
        }

    def materialize_notebook(
        self,
        *,
        workspace_path: str,
        runtime_profile: str,
        cells: list[dict[str, Any]],
    ) -> dict[str, Any]:
        profile = _runtime_profile(runtime_profile)
        if not isinstance(cells, list) or not cells:
            raise OutputJournalError("cells must not be empty.")
        workspace = self._resolve_workspace(workspace_path, must_exist=True)
        notebook_path = workspace / "notebooks" / "execution.ipynb"
        materialized_cells: list[dict[str, Any]] = []
        output_count = 0
        seen_sequences: set[int] = set()
        for cell in cells:
            if not isinstance(cell, dict):
                raise OutputJournalError(
                    "Each notebook cell must be an object."
                )
            try:
                sequence = int(cell["sequence"])
                journal_id = _uuid_string(cell["journal_id"], "journal_id")
                identity_value = cell["journal"]
            except (KeyError, TypeError, ValueError) as exc:
                raise OutputJournalError(
                    "Notebook cell materialization input is invalid."
                ) from exc
            if sequence < 0 or sequence in seen_sequences:
                raise OutputJournalError(
                    "Notebook cell sequences must be unique and non-negative."
                )
            if not isinstance(identity_value, dict):
                raise OutputJournalError(
                    "Notebook cell journal must be an object."
                )
            identity = JournalIdentity.from_mapping(identity_value)
            if identity.workspace_path != workspace_path:
                raise OutputJournalConflictError(
                    "Notebook cell journal belongs to another workspace."
                )
            directory = self._journal_directory(identity, must_exist=True)
            state, batches, header = self._load_journal(directory)
            self._require_journal(state, identity, journal_id)
            if state["state"] not in TERMINAL_STATES:
                raise OutputJournalConflictError(
                    "Notebook materialization requires a terminal Output Journal."
                )
            source = self._journal_source(header, state)
            notebook_outputs = [
                self._notebook_output(directory, output)
                for batch in batches
                for output in batch["outputs"]
            ]
            if len(notebook_outputs) != state["output_count"]:
                raise OutputJournalConflictError(
                    "Output Journal count changed during notebook materialization."
                )
            execution_count = cell.get("execution_count")
            if execution_count is not None:
                try:
                    execution_count = int(execution_count)
                except (TypeError, ValueError) as exc:
                    raise OutputJournalError(
                        "Notebook cell execution_count must be an integer."
                    ) from exc
                if execution_count < 0:
                    raise OutputJournalError(
                        "Notebook cell execution_count must be non-negative."
                    )
            if execution_count is None:
                execution_count = _output_execution_count(
                    notebook_outputs, sequence
                )
            materialized_cells.append(
                {
                    "execution_count": execution_count,
                    "id": _notebook_cell_id(
                        identity.execution_id, identity.step_id
                    ),
                    "execution_id": identity.execution_id,
                    "operation_id": identity.operation_id,
                    "step_id": identity.step_id,
                    "sequence": sequence,
                    "journal_id": journal_id,
                    "journal_state": state["state"],
                    "fencing_token": identity.fencing_token,
                    "outputs": notebook_outputs,
                    "source": source,
                }
            )
            output_count += len(notebook_outputs)
            seen_sequences.add(sequence)
        lock = self._lock_for(notebook_path)
        with lock:
            notebook = _load_prepared_notebook(
                notebook_path,
                workspace_path=workspace_path,
                runtime_profile=profile,
            )
            notebook_cells = {
                str(cell.get("id")): cell for cell in notebook["cells"]
            }
            for value in materialized_cells:
                cell = notebook_cells.get(value["id"])
                if cell is None:
                    raise OutputJournalConflictError(
                        "Output Journal has no prepared notebook cell."
                    )
                _require_materialized_notebook_cell(cell, value)
                metadata = _executor_cell_metadata(cell)
                if metadata is None:
                    raise OutputJournalConflictError(
                        "Prepared notebook cell metadata is unavailable."
                    )
                current_fencing_token = metadata.get("fencing_token")
                if (
                    type(current_fencing_token) is int
                    and current_fencing_token > value["fencing_token"]
                ):
                    raise OutputJournalConflictError(
                        "A stale Output Journal cannot replace a newer notebook cell."
                    )
                if current_fencing_token == value[
                    "fencing_token"
                ] and metadata.get("journal_id") not in {
                    None,
                    value["journal_id"],
                }:
                    raise OutputJournalConflictError(
                        "A fencing token cannot identify multiple notebook Journals."
                    )
                metadata.update(
                    {
                        "journal_id": value["journal_id"],
                        "journal_state": value["journal_state"],
                        "fencing_token": value["fencing_token"],
                    }
                )
                cell["execution_count"] = value["execution_count"]
                cell["outputs"] = value["outputs"]
            _atomic_json_write(notebook_path, notebook)
        return {
            "notebook_path": notebook_path.relative_to(self._root).as_posix(),
            "cell_count": len(materialized_cells),
            "output_count": output_count,
        }

    @staticmethod
    def _journal_source(header: dict[str, Any], state: dict[str, Any]) -> str:
        try:
            expected_size = int(state["source_size_bytes"])
            expected_checksum = str(state["source_checksum_sha256"])
            source = str(header["source"])
            body = source.encode("utf-8")
        except (KeyError, UnicodeEncodeError, TypeError, ValueError) as exc:
            raise OutputJournalConflictError(
                "Output Journal source is unavailable."
            ) from exc
        if (
            len(body) != expected_size
            or _sha256_bytes(body) != expected_checksum
        ):
            raise OutputJournalConflictError(
                "Output Journal source does not match metadata."
            )
        if not source.strip():
            raise OutputJournalConflictError(
                "Output Journal source must not be blank."
            )
        return source

    def _notebook_output(
        self, directory: Path, output: dict[str, Any]
    ) -> dict[str, Any]:
        kind = str(output.get("kind", ""))
        representations = output.get("representations")
        if not isinstance(representations, list):
            raise OutputJournalConflictError(
                "Output Journal representations are invalid."
            )
        values = {
            str(representation["media_type"]): self._representation_value(
                directory, representation
            )
            for representation in representations
        }
        metadata = output.get("metadata", {})
        if not isinstance(metadata, dict):
            raise OutputJournalConflictError(
                "Output Journal metadata is invalid."
            )
        if kind == "STREAM":
            return {
                "output_type": "stream",
                "name": str(output.get("stream_name") or "stdout"),
                "text": str(values.get("text/plain", "")),
            }
        if kind in {"DISPLAY", "RESULT"}:
            result: dict[str, Any] = {
                "output_type": (
                    "display_data" if kind == "DISPLAY" else "execute_result"
                ),
                "data": values,
                "metadata": {
                    key: value
                    for key, value in metadata.items()
                    if key != "transient"
                },
            }
            if kind == "RESULT":
                result["execution_count"] = output.get("execution_count")
            return result
        if kind == "ERROR":
            traceback = str(values.get("text/plain", "")).splitlines()
            return {
                "output_type": "error",
                "ename": str(metadata.get("ename", "Error")),
                "evalue": str(metadata.get("evalue", "")),
                "traceback": traceback,
            }
        raise OutputJournalConflictError(
            f"Unsupported persisted output kind: {kind!r}."
        )

    def _representation_value(
        self, directory: Path, representation: dict[str, Any]
    ) -> Any:
        try:
            media_type = str(representation["media_type"])
            size_bytes = int(representation["size_bytes"])
            checksum = str(representation["checksum_sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OutputJournalConflictError(
                "Output representation metadata is invalid."
            ) from exc
        storage_path = representation.get("storage_path")
        inline_content = representation.get("inline_content")
        encoding = str(representation.get("encoding", ""))
        if storage_path is not None:
            path = (self._root / str(storage_path)).resolve(strict=True)
            self._ensure_within_root(path)
            try:
                path.relative_to(directory)
            except ValueError as exc:
                raise OutputJournalConflictError(
                    "Output representation escapes its journal."
                ) from exc
            body = path.read_bytes()
        elif isinstance(inline_content, str):
            try:
                body = (
                    inline_content.encode("utf-8")
                    if encoding == "UTF8"
                    else base64.b64decode(inline_content, validate=True)
                )
            except (UnicodeEncodeError, binascii.Error) as exc:
                raise OutputJournalConflictError(
                    "Inline output representation is invalid."
                ) from exc
        else:
            raise OutputJournalConflictError(
                "Output representation content is unavailable."
            )
        if len(body) != size_bytes or _sha256_bytes(body) != checksum:
            raise OutputJournalConflictError(
                "Output representation content does not match metadata."
            )
        if encoding == "BASE64":
            return base64.b64encode(body).decode("ascii")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OutputJournalConflictError(
                "Non-binary output representation is not UTF-8."
            ) from exc
        if media_type == "application/json" or media_type.endswith("+json"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        return text

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
        metadata = value.get("metadata", {})
        if not isinstance(metadata, dict):
            raise OutputJournalError(
                "representation metadata must be an object."
            )
        persisted = {
            "representation_id": representation_id,
            "media_type": media_type,
            "encoding": encoding,
            "size_bytes": len(body),
            "checksum_sha256": _sha256_bytes(body),
            "complete": True,
            "content_ref": (
                f"journal://{journal_id}/{output_id}/{representation_id}"
            ),
            "metadata": _json_object(metadata, "representation metadata"),
        }
        image_extension = IMAGE_EXTENSIONS.get(media_type)
        if image_extension is None and media_type.startswith("image/"):
            image_extension = ".img"
        if image_extension is None:
            persisted["inline_content"] = content
            return persisted

        image_directory = directory / "images"
        image_directory.mkdir(parents=True, exist_ok=True)
        image_path = image_directory / f"{representation_id}{image_extension}"
        if image_path.exists():
            existing = image_path.read_bytes()
            if existing != body:
                raise OutputJournalConflictError(
                    "Image output conflicts with existing journal content."
                )
        else:
            _atomic_bytes_write(image_path, body)
        persisted["storage_path"] = image_path.relative_to(
            self._root
        ).as_posix()
        return persisted

    def _load_journal(
        self, directory: Path
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        entries = _read_json_lines(directory / JOURNAL_FILE_NAME)
        header = entries[0]
        if (
            header.get("schema_version") != JOURNAL_SCHEMA_VERSION
            or header.get("record_type") != HEADER_RECORD
        ):
            raise OutputJournalConflictError(
                "Output Journal header is invalid."
            )
        batches: list[dict[str, Any]] = []
        terminal: dict[str, Any] | None = None
        seen_batch_ids: set[str] = set()
        for entry in entries[1:]:
            if entry.get("schema_version") != JOURNAL_SCHEMA_VERSION:
                raise OutputJournalConflictError(
                    "Output Journal schema_version is unsupported."
                )
            record_type = entry.get("record_type")
            if record_type == BATCH_RECORD and terminal is None:
                raw_batch_id = entry.get("batch_id")
                if not isinstance(raw_batch_id, str):
                    raise OutputJournalConflictError(
                        "Output Journal batch_id is invalid."
                    )
                batch_id = _uuid_string(raw_batch_id, "batch_id")
                if batch_id in seen_batch_ids:
                    raise OutputJournalConflictError(
                        "Output Journal contains a duplicate batch_id."
                    )
                seen_batch_ids.add(batch_id)
                batches.append(entry)
                continue
            if record_type == TERMINAL_RECORD and terminal is None:
                if entry.get("state") not in TERMINAL_STATES:
                    raise OutputJournalConflictError(
                        "Output Journal terminal state is invalid."
                    )
                terminal = entry
                continue
            raise OutputJournalConflictError(
                "Output Journal record ordering is invalid."
            )

        expected_offset = 0
        representation_count = 0
        total_bytes = 0
        for batch in batches:
            outputs = batch.get("outputs")
            if not isinstance(outputs, list):
                raise OutputJournalConflictError(
                    "Output Journal batch outputs are invalid."
                )
            if (
                batch.get("start_offset") != expected_offset
                or batch.get("end_offset") != expected_offset + len(outputs)
                or batch.get("output_count") != len(outputs)
            ):
                raise OutputJournalConflictError(
                    "Output Journal batch offsets are not contiguous."
                )
            expected_offset = int(batch["end_offset"])
            batch_representation_count = sum(
                len(output.get("representations", [])) for output in outputs
            )
            batch_total_bytes = sum(
                int(representation.get("size_bytes", -1))
                for output in outputs
                for representation in output.get("representations", [])
            )
            if (
                batch.get("representation_count") != batch_representation_count
                or batch.get("total_bytes") != batch_total_bytes
                or batch_total_bytes < 0
            ):
                raise OutputJournalConflictError(
                    "Output Journal batch metadata is inconsistent."
                )
            representation_count += batch_representation_count
            total_bytes += batch_total_bytes

        if terminal is not None:
            expected_checksum = self._journal_checksum(header, batches)
            if terminal.get("checksum_sha256") != expected_checksum:
                raise OutputJournalConflictError(
                    "Output Journal terminal checksum is invalid."
                )
        state = self._state_from_entries(
            header,
            batches,
            terminal,
            committed_offset=expected_offset,
            representation_count=representation_count,
            total_bytes=total_bytes,
        )
        return state, batches, header

    @staticmethod
    def _state_from_entries(
        header: dict[str, Any],
        batches: list[dict[str, Any]],
        terminal: dict[str, Any] | None,
        *,
        committed_offset: int | None = None,
        representation_count: int | None = None,
        total_bytes: int | None = None,
    ) -> dict[str, Any]:
        if committed_offset is None:
            committed_offset = sum(
                int(batch.get("output_count", 0)) for batch in batches
            )
        if representation_count is None:
            representation_count = sum(
                int(batch.get("representation_count", 0)) for batch in batches
            )
        if total_bytes is None:
            total_bytes = sum(
                int(batch.get("total_bytes", 0)) for batch in batches
            )
        updated_at = (terminal or (batches[-1] if batches else header)).get(
            "created_at"
        )
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "journal_id": header.get("journal_id"),
            "state": terminal.get("state") if terminal else "OPEN",
            "identity": header.get("identity"),
            "source_size_bytes": header.get("source_size_bytes"),
            "source_checksum_sha256": header.get("source_checksum_sha256"),
            "committed_offset": committed_offset,
            "batch_count": len(batches),
            "output_count": committed_offset,
            "representation_count": representation_count,
            "total_bytes": total_bytes,
            "checksum_sha256": (
                terminal.get("checksum_sha256") if terminal else None
            ),
            "abort_reason": terminal.get("abort_reason") if terminal else None,
            "created_at": header.get("created_at"),
            "updated_at": updated_at,
            "completed_at": terminal.get("created_at") if terminal else None,
        }

    @staticmethod
    def _journal_checksum(
        header: dict[str, Any], batches: list[dict[str, Any]]
    ) -> str:
        digest = hashlib.sha256()
        digest.update(_canonical_json(header))
        for batch in batches:
            digest.update(b"\n")
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
                            if key
                            not in {
                                "encoding",
                                "inline_content",
                                "storage_path",
                            }
                        }
                        for representation in output["representations"]
                    ],
                }
                for output in batch["outputs"]
            ],
            "replayed": replayed,
        }


def _runtime_profile(value: str) -> str:
    profile = value.strip()
    if not 1 <= len(profile) <= 128:
        raise OutputJournalError(
            "runtime_profile must contain 1 to 128 characters."
        )
    return profile


def _notebook_cell_id(execution_id: str, step_id: str) -> str:
    return uuid5(NAMESPACE_URL, f"{execution_id}:cell:{step_id}").hex[:16]


def _prepared_notebook_cell(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "cell_type": "code",
        "execution_count": None,
        "id": value["cell_id"],
        "metadata": {
            "executor": {
                "execution_id": value["execution_id"],
                "operation_id": value["operation_id"],
                "step_id": value["step_id"],
                "sequence": value["sequence"],
            }
        },
        "outputs": [],
        "source": value["source"],
    }


def _executor_cell_metadata(
    cell: dict[str, Any],
) -> dict[str, Any] | None:
    metadata = cell.get("metadata")
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("executor")
    return value if isinstance(value, dict) else None


def _require_prepared_notebook_cell(
    cell: dict[str, Any], value: dict[str, Any]
) -> None:
    metadata = _executor_cell_metadata(cell)
    if (
        cell.get("cell_type") != "code"
        or cell.get("source") != value["source"]
        or metadata is None
        or any(
            metadata.get(key) != value[key]
            for key in (
                "execution_id",
                "operation_id",
                "step_id",
                "sequence",
            )
        )
    ):
        raise OutputJournalConflictError(
            "Prepared notebook cell conflicts with its Execution Step."
        )


def _require_materialized_notebook_cell(
    cell: dict[str, Any], value: dict[str, Any]
) -> None:
    metadata = _executor_cell_metadata(cell)
    if (
        cell.get("cell_type") != "code"
        or cell.get("source") != value["source"]
        or metadata is None
        or any(
            str(metadata.get(key)) != str(value[key])
            for key in (
                "execution_id",
                "operation_id",
                "step_id",
                "sequence",
            )
        )
    ):
        raise OutputJournalConflictError(
            "Output Journal conflicts with its prepared notebook cell."
        )


def _notebook_cell_sort_key(cell: dict[str, Any]) -> tuple[int, int]:
    metadata = _executor_cell_metadata(cell)
    sequence = metadata.get("sequence") if metadata is not None else None
    if type(sequence) is int and sequence >= 0:
        return 0, sequence
    return 1, 0


def _load_or_create_notebook(
    path: Path,
    *,
    workspace_path: str,
    execution_id: str,
    runtime_profile: str,
) -> dict[str, Any]:
    if not path.exists():
        return {
            "cells": [],
            "metadata": {
                "executor": {
                    "workspace": workspace_path,
                    "execution_id": execution_id,
                },
                "kernelspec": {
                    "display_name": runtime_profile,
                    "language": "python",
                    "name": runtime_profile,
                },
                "language_info": {"name": "python"},
            },
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    notebook = _load_prepared_notebook(
        path,
        workspace_path=workspace_path,
        runtime_profile=runtime_profile,
    )
    metadata = notebook["metadata"]["executor"]
    if metadata.get("execution_id") != execution_id:
        raise OutputJournalConflictError(
            "Notebook belongs to another Execution."
        )
    return notebook


def _load_prepared_notebook(
    path: Path, *, workspace_path: str, runtime_profile: str
) -> dict[str, Any]:
    if not path.is_file():
        raise OutputJournalConflictError(
            "Prepared execution notebook was not found."
        )
    notebook = _read_json(path)
    metadata = notebook.get("metadata")
    cells = notebook.get("cells")
    if (
        notebook.get("nbformat") != 4
        or not isinstance(metadata, dict)
        or not isinstance(cells, list)
        or not all(isinstance(cell, dict) for cell in cells)
    ):
        raise OutputJournalConflictError(
            "Prepared execution notebook is invalid."
        )
    executor_metadata = metadata.get("executor")
    kernelspec = metadata.get("kernelspec")
    if (
        not isinstance(executor_metadata, dict)
        or executor_metadata.get("workspace") != workspace_path
        or not isinstance(kernelspec, dict)
        or kernelspec.get("name") != runtime_profile
    ):
        raise OutputJournalConflictError(
            "Prepared execution notebook metadata does not match."
        )
    return notebook


def _uuid_string(value: str, name: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise OutputJournalError(f"{name} must be a UUID.") from exc


def _output_execution_count(
    outputs: list[dict[str, Any]], sequence: int
) -> int:
    counts = [
        value
        for output in outputs
        if type(value := output.get("execution_count")) is int
    ]
    return max(counts, default=sequence + 1)


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


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise OutputJournalNotFoundError("Output Journal was not found.")
    try:
        with path.open("r+b") as handle:
            body = handle.read()
            if body and not body.endswith(b"\n"):
                committed_end = body.rfind(b"\n") + 1
                if committed_end <= 0:
                    raise OutputJournalConflictError(
                        "Output Journal header is incomplete."
                    )
                handle.seek(committed_end)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
                body = body[:committed_end]
    except OSError as exc:
        raise OutputJournalConflictError(
            "Output Journal storage cannot be read."
        ) from exc
    entries: list[dict[str, Any]] = []
    try:
        for line in body.splitlines():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise OutputJournalConflictError(
                    "Output Journal records must be JSON objects."
                )
            entries.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OutputJournalConflictError(
            "Output Journal contains invalid JSONL."
        ) from exc
    if not entries:
        raise OutputJournalConflictError("Output Journal is empty.")
    return entries


def _append_json_line(path: Path, value: dict[str, Any]) -> None:
    content = _canonical_json(value) + b"\n"
    try:
        with path.open("ab") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise OutputJournalConflictError(
            "Output Journal append failed."
        ) from exc


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
