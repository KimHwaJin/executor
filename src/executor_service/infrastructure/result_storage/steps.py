"""State machine for immutable Step result directories."""

import json
import os
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

from executor_service.domain.results import (
    RESULT_MANIFEST_SCHEMA_VERSION,
    ExecutionSourceReference,
    StepResultAppend,
    StepResultDescriptor,
    StepResultIdentity,
    StepResultProjection,
    StepResultReference,
)
from executor_service.domain.runtime import RuntimeOutputRecord
from executor_service.infrastructure.result_storage.codec import (
    ResultOutputCodec,
    identity_json,
    output_summary,
    records_digest,
    source_json,
)
from executor_service.infrastructure.result_storage.errors import (
    ResultStorageError,
)
from executor_service.infrastructure.result_storage.io import (
    atomic_json_write,
    atomic_write,
    fsync_directory,
    sha256,
    utc_now,
)
from executor_service.infrastructure.result_storage.paths import (
    ResultStoragePaths,
)
from executor_service.infrastructure.result_storage.sources import (
    FilesystemExecutionSourceStore,
)

TERMINAL_STATES = frozenset({"FINALIZED", "FAILED", "ABORTED"})
# One JSON line per appended batch while a result is OPEN.
OUTPUT_LOG = "outputs.jsonl"


class FilesystemStepResultStore:
    def __init__(
        self,
        paths: ResultStoragePaths,
        sources: FilesystemExecutionSourceStore,
        codec: ResultOutputCodec,
    ) -> None:
        self._paths = paths
        self._sources = sources
        self._codec = codec
        # Only the fenced owner appends to a partial result, so its counters
        # can stay in memory between appends.
        self._logs: dict[Path, _OutputLog] = {}

    def read_projection(
        self, reference: StepResultReference
    ) -> StepResultProjection:
        manifest_path = self._paths.resolve_reference(reference.relative_path)
        body = manifest_path.read_bytes()
        if sha256(body) != reference.checksum_sha256:
            raise ResultStorageError("Step result manifest checksum failed.")
        try:
            manifest = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ResultStorageError(
                "Step result manifest is invalid JSON."
            ) from exc
        identity = manifest.get("identity")
        if not isinstance(identity, dict) or (
            str(identity.get("execution_attempt_id"))
            != str(reference.execution_attempt_id)
            or identity.get("fencing_token") != reference.fencing_token
        ):
            raise ResultStorageError(
                "Step result reference identity conflicts."
            )
        outputs = manifest.get("outputs")
        if not isinstance(outputs, list):
            raise ResultStorageError("Step result outputs are invalid.")
        execution_count = manifest.get("execution_count")
        if execution_count is not None and type(execution_count) is not int:
            raise ResultStorageError("Step result execution count is invalid.")
        return StepResultProjection(
            outputs=[
                self._codec.notebook_output(manifest_path.parent, output)
                for output in outputs
            ],
            execution_count=execution_count,
        )

    def begin(
        self,
        identity: StepResultIdentity,
        source: ExecutionSourceReference,
    ) -> None:
        final = self._paths.result_directory(identity)
        partial = self._paths.partial_directory(identity)
        if final.exists():
            descriptor = self.load_terminal_descriptor(final)
            if descriptor.source != source:
                raise ResultStorageError(
                    "Terminal Step result belongs to different source content."
                )
            return
        if partial.exists():
            state = _load_state(partial)
            if state.get("identity") != identity_json(identity):
                raise ResultStorageError("Step result identity conflicts.")
            if state.get("source") != source_json(source):
                raise ResultStorageError(
                    "Open Step result belongs to different source content."
                )
            return
        partial.mkdir(parents=True, exist_ok=False)
        source_body = self._sources.read(source).encode("utf-8")
        atomic_write(partial / "source.py", source_body)
        (partial / "outputs").mkdir()
        now = utc_now()
        state = {
            "schema_version": RESULT_MANIFEST_SCHEMA_VERSION,
            "state": "OPEN",
            "complete": False,
            "identity": identity_json(identity),
            "source": source_json(source),
            "created_at": now,
        }
        atomic_write(partial / OUTPUT_LOG, b"")
        atomic_json_write(partial / ".state.json", state)

    def append(
        self,
        identity: StepResultIdentity,
        expected_offset: int,
        batch_id: UUID,
        records: tuple[RuntimeOutputRecord, ...],
    ) -> StepResultAppend:
        if not records:
            raise ResultStorageError("Step output batch must not be empty.")
        partial = self._paths.partial_directory(identity, must_exist=True)
        log = self._open_log(partial, identity)
        request_digest = records_digest(records)
        batch_key = str(batch_id)
        existing_batch = log.batches.get(batch_key)
        if existing_batch is not None:
            if existing_batch["request_sha256"] != request_digest:
                raise ResultStorageError(
                    "Step result batch ID conflicts with existing output."
                )
            return StepResultAppend(
                committed_offset=existing_batch["end_offset"],
                output_count=existing_batch["output_count"],
                representation_count=existing_batch["representation_count"],
                total_size_bytes=existing_batch["total_size_bytes"],
                replayed=True,
            )
        if expected_offset != log.output_count:
            raise ResultStorageError(
                "Step result expected offset does not match committed output."
            )
        outputs = [
            self._codec.persist_record(
                partial,
                result_relative=self._paths.result_relative(identity),
                ordinal=expected_offset + index,
                record=record,
            )
            for index, record in enumerate(records)
        ]
        entry = {
            "batch_id": batch_key,
            "request_sha256": request_digest,
            "outputs": outputs,
        }
        # Append one line and fsync it: the cost of a batch no longer grows
        # with the output already written.
        with (partial / OUTPUT_LOG).open("ab") as handle:
            handle.write(_log_line(entry))
            handle.flush()
            os.fsync(handle.fileno())
        batch = log.add(entry)
        return StepResultAppend(
            committed_offset=batch["end_offset"],
            output_count=batch["output_count"],
            representation_count=batch["representation_count"],
            total_size_bytes=batch["total_size_bytes"],
            replayed=False,
        )

    def seal(
        self,
        identity: StepResultIdentity,
        state_name: str,
        execution_count: int | None,
        error_message: str | None,
    ) -> StepResultDescriptor:
        if state_name not in TERMINAL_STATES:
            raise ResultStorageError("Unsupported terminal result state.")
        final = self._paths.result_directory(identity)
        if final.exists():
            descriptor = self.load_terminal_descriptor(final)
            if descriptor.state != state_name:
                raise ResultStorageError(
                    "Terminal Step result state conflicts with replay."
                )
            return descriptor
        partial = self._paths.partial_directory(identity, must_exist=True)
        log = self._open_log(partial, identity)
        outputs = [
            output
            for entry in _read_log(partial / OUTPUT_LOG)
            for output in entry["outputs"]
        ]
        completed_at = utc_now()
        manifest = {
            **log.header,
            "state": state_name,
            "complete": state_name in {"FINALIZED", "FAILED"},
            "outputs": outputs,
            "output_count": log.output_count,
            "representation_count": log.representation_count,
            "total_size_bytes": log.total_size_bytes,
            "execution_count": execution_count,
            "error_message": (
                error_message[:2000] if error_message is not None else None
            ),
            "output_summary": output_summary(outputs),
            "updated_at": completed_at,
            "completed_at": completed_at,
        }
        atomic_json_write(partial / "manifest.json", manifest)
        (partial / ".state.json").unlink()
        (partial / OUTPUT_LOG).unlink()
        self._logs.pop(partial, None)
        fsync_directory(partial)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(partial, final)
        fsync_directory(final.parent)
        return self.load_terminal_descriptor(final)

    def load_terminal_descriptor(
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
        complete = value.get("complete")
        if type(complete) is not bool:
            raise ResultStorageError(
                "Step result manifest completeness is invalid."
            )
        identity = value.get("identity")
        source = value.get("source")
        if not isinstance(identity, dict) or not isinstance(source, dict):
            raise ResultStorageError(
                "Step result manifest identity is invalid."
            )
        relative = manifest_path.relative_to(self._paths.root).as_posix()
        return StepResultDescriptor(
            state=state,
            complete=complete,
            reference=StepResultReference(
                relative_path=relative,
                checksum_sha256=sha256(body),
                size_bytes=len(body),
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

    def _open_log(
        self, partial: Path, identity: StepResultIdentity
    ) -> "_OutputLog":
        """The OPEN result's counters, read from disk once per writer."""

        log = self._logs.get(partial)
        if log is None:
            header = _load_state(partial)
            if header.get("state") != "OPEN":
                raise ResultStorageError("Step result is not OPEN.")
            log = _OutputLog(header)
            for entry in _read_log(partial / OUTPUT_LOG, repair=True):
                log.add(entry)
            self._logs[partial] = log
        if log.header.get("identity") != identity_json(identity):
            raise ResultStorageError("Step result identity conflicts.")
        return log


class _OutputLog:
    """Counters and batch receipts of one OPEN Step result."""

    def __init__(self, header: dict[str, Any]) -> None:
        self.header = header
        self.output_count = 0
        self.representation_count = 0
        self.total_size_bytes = 0
        self.batches: dict[str, dict[str, Any]] = {}

    def add(self, entry: dict[str, Any]) -> dict[str, Any]:
        outputs = entry["outputs"]
        representations = [
            item for output in outputs for item in output["representations"]
        ]
        batch = {
            "request_sha256": entry["request_sha256"],
            "end_offset": self.output_count + len(outputs),
            "output_count": len(outputs),
            "representation_count": len(representations),
            "total_size_bytes": sum(
                int(item["size_bytes"]) for item in representations
            ),
        }
        self.output_count = batch["end_offset"]
        self.representation_count += batch["representation_count"]
        self.total_size_bytes += batch["total_size_bytes"]
        self.batches[entry["batch_id"]] = batch
        return batch


def _log_line(entry: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        + b"\n"
    )


def _read_log(path: Path, *, repair: bool = False) -> list[dict[str, Any]]:
    try:
        body = path.read_bytes()
    except FileNotFoundError as exc:
        raise ResultStorageError(
            "Open Step result output log is unavailable."
        ) from exc
    complete = body[: body.rfind(b"\n") + 1]
    if repair and len(complete) != len(body):
        # A crash mid-append leaves a partial last line that was never
        # acknowledged; drop it so the next append starts on a clean line.
        with path.open("r+b") as handle:
            handle.truncate(len(complete))
            os.fsync(handle.fileno())
    try:
        return [json.loads(line) for line in complete.splitlines()]
    except json.JSONDecodeError as exc:
        raise ResultStorageError(
            "Open Step result output log is invalid."
        ) from exc


def _load_state(directory: Path) -> dict[str, Any]:
    try:
        value = json.loads((directory / ".state.json").read_bytes())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise ResultStorageError(
            "Open Step result state is unavailable."
        ) from exc
    if not isinstance(value, dict):
        raise ResultStorageError("Open Step result state must be an object.")
    return value


def remove_partial_files(root: Path, identity: StepResultIdentity) -> None:
    """Remove a partial result after its fencing lease is proven inactive."""

    partial = ResultStoragePaths(root).partial_directory(identity)
    if partial.exists():
        shutil.rmtree(partial)
