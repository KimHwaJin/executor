"""State machine for immutable Step result directories."""

import json
import os
import shutil
from dataclasses import dataclass, field
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
    source_json,
)
from executor_service.infrastructure.result_storage.errors import (
    ResultStorageError,
)
from executor_service.infrastructure.result_storage.io import (
    atomic_json_write,
    fsync_directory,
    sha256,
    utc_now,
)
from executor_service.infrastructure.result_storage.paths import (
    ResultStoragePaths,
)

TERMINAL_STATES = frozenset({"FINALIZED", "FAILED", "ABORTED"})


@dataclass(slots=True)
class _OpenResult:
    """One Step result between ``begin`` and ``seal``."""

    identity: StepResultIdentity
    source: ExecutionSourceReference
    created_at: str
    outputs: list[dict[str, Any]] = field(default_factory=list)
    representation_count: int = 0
    total_size_bytes: int = 0


class FilesystemStepResultStore:
    """Writes a Step result into a partial directory and seals it.

    An open result lives in this process only. Its partial directory is
    written and sealed by the one Worker holding that fencing token and is
    never resumed: a Worker that takes the Step over has a new token, so a
    new directory, and reads the output again from the Jupyter journal.
    Nothing is fsynced for the directory until ``seal``; output file
    content is fsynced as it is appended.
    """

    def __init__(
        self,
        paths: ResultStoragePaths,
        codec: ResultOutputCodec,
    ) -> None:
        self._paths = paths
        self._codec = codec
        self._open: dict[Path, _OpenResult] = {}

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
        opened = self._open.get(partial)
        if opened is not None:
            if opened.identity != identity:
                raise ResultStorageError("Step result identity conflicts.")
            if opened.source != source:
                raise ResultStorageError(
                    "Open Step result belongs to different source content."
                )
            return
        if partial.exists():
            # Left by a process that is gone; it is never authoritative.
            shutil.rmtree(partial)
        (partial / "outputs").mkdir(parents=True)
        self._open[partial] = _OpenResult(
            identity=identity, source=source, created_at=utc_now()
        )

    def append(
        self,
        identity: StepResultIdentity,
        expected_offset: int,
        records: tuple[RuntimeOutputRecord, ...],
    ) -> StepResultAppend:
        if not records:
            raise ResultStorageError("Step output batch must not be empty.")
        partial = self._paths.partial_directory(identity)
        opened = self._opened(partial, identity)
        if expected_offset != len(opened.outputs):
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
        representations = [
            item for output in outputs for item in output["representations"]
        ]
        opened.outputs.extend(outputs)
        opened.representation_count += len(representations)
        opened.total_size_bytes += sum(
            int(item["size_bytes"]) for item in representations
        )
        return StepResultAppend(
            committed_offset=len(opened.outputs),
            output_count=len(outputs),
            representation_count=len(representations),
            total_size_bytes=sum(
                int(item["size_bytes"]) for item in representations
            ),
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
        partial = self._paths.partial_directory(identity)
        opened = self._opened(partial, identity)
        completed_at = utc_now()
        manifest = {
            "schema_version": RESULT_MANIFEST_SCHEMA_VERSION,
            "identity": identity_json(identity),
            "source": source_json(opened.source),
            "created_at": opened.created_at,
            "state": state_name,
            "complete": state_name in {"FINALIZED", "FAILED"},
            "outputs": opened.outputs,
            "output_count": len(opened.outputs),
            "representation_count": opened.representation_count,
            "total_size_bytes": opened.total_size_bytes,
            "execution_count": execution_count,
            "error_message": (
                error_message[:2000] if error_message is not None else None
            ),
            "output_summary": output_summary(opened.outputs),
            "updated_at": completed_at,
            "completed_at": completed_at,
        }
        # Everything the sealed result needs becomes durable here.
        atomic_json_write(
            partial / "manifest.json", manifest, sync_directory=False
        )
        fsync_directory(partial / "outputs")
        fsync_directory(partial)
        os.replace(partial, final)
        fsync_directory(final.parent)
        del self._open[partial]
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

    def _opened(
        self, partial: Path, identity: StepResultIdentity
    ) -> _OpenResult:
        opened = self._open.get(partial)
        if opened is None:
            raise ResultStorageError("Step result is not OPEN.")
        if opened.identity != identity:
            raise ResultStorageError("Step result identity conflicts.")
        return opened


def remove_partial_files(root: Path, identity: StepResultIdentity) -> None:
    """Remove a partial result after its fencing lease is proven inactive."""

    partial = ResultStoragePaths(root).partial_directory(identity)
    if partial.exists():
        shutil.rmtree(partial)
