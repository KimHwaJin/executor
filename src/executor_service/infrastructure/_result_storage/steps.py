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
from executor_service.infrastructure._result_storage.codec import (
    ResultOutputCodec,
    identity_json,
    output_summary,
    records_digest,
    source_json,
)
from executor_service.infrastructure._result_storage.errors import (
    ResultStorageError,
)
from executor_service.infrastructure._result_storage.io import (
    atomic_json_write,
    atomic_write,
    fsync_directory,
    sha256,
    utc_now,
)
from executor_service.infrastructure._result_storage.paths import (
    ResultStoragePaths,
)
from executor_service.infrastructure._result_storage.sources import (
    FilesystemExecutionSourceStore,
)

TERMINAL_STATES = frozenset({"FINALIZED", "FAILED", "ABORTED"})


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
            state = self._load_state(partial)
            self._require_identity(state, identity)
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
        state = self._load_state(partial)
        self._require_identity(state, identity)
        if state.get("state") != "OPEN":
            raise ResultStorageError(
                "Only an OPEN Step result accepts output."
            )
        request_digest = records_digest(records)
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
            output = self._codec.persist_record(
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
        state["updated_at"] = utc_now()
        state["batches"][batch_key] = {
            "request_sha256": request_digest,
            "start_offset": expected_offset,
            "end_offset": state["output_count"],
            "output_count": len(outputs),
            "representation_count": representation_count,
            "total_size_bytes": total_size_bytes,
        }
        atomic_json_write(partial / ".state.json", state)
        return StepResultAppend(
            committed_offset=int(state["output_count"]),
            output_count=len(outputs),
            representation_count=representation_count,
            total_size_bytes=total_size_bytes,
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
        state = self._load_state(partial)
        self._require_identity(state, identity)
        if state.get("state") != "OPEN":
            raise ResultStorageError("Step result is not OPEN.")
        state["state"] = state_name
        state["complete"] = state_name in {"FINALIZED", "FAILED"}
        state["execution_count"] = execution_count
        state["error_message"] = (
            error_message[:2000] if error_message is not None else None
        )
        state["output_summary"] = output_summary(state["outputs"])
        state["completed_at"] = utc_now()
        state["updated_at"] = state["completed_at"]
        state.pop("batches", None)
        atomic_json_write(partial / "manifest.json", state)
        (partial / ".state.json").unlink()
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

    @staticmethod
    def _require_identity(
        state: dict[str, Any], identity: StepResultIdentity
    ) -> None:
        if state.get("identity") != identity_json(identity):
            raise ResultStorageError("Step result identity conflicts.")

    @staticmethod
    def _load_state(directory: Path) -> dict[str, Any]:
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


def remove_partial_files(root: Path, identity: StepResultIdentity) -> None:
    """Remove a partial result after its fencing lease is proven inactive."""

    partial = ResultStoragePaths(root).partial_directory(identity)
    if partial.exists():
        shutil.rmtree(partial)
