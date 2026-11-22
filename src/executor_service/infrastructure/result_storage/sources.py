"""Immutable Execution source snapshots in shared storage."""

from uuid import UUID

from executor_service.domain.results import ExecutionSourceReference
from executor_service.infrastructure.result_storage.errors import (
    ResultStorageError,
)
from executor_service.infrastructure.result_storage.io import (
    atomic_write,
    fsync_directory,
    sha256,
)
from executor_service.infrastructure.result_storage.paths import (
    ResultStoragePaths,
)


class FilesystemExecutionSourceStore:
    def __init__(self, paths: ResultStoragePaths) -> None:
        self._paths = paths

    def snapshot(
        self,
        execution_id: UUID,
        operation_id: UUID,
        step_id: UUID,
        content: str,
    ) -> ExecutionSourceReference:
        reference = self.write(execution_id, operation_id, step_id, content)
        self.sync_directory(execution_id, operation_id)
        return reference

    def write(
        self,
        execution_id: UUID,
        operation_id: UUID,
        step_id: UUID,
        content: str,
    ) -> ExecutionSourceReference:
        """Write one snapshot; durable once ``sync_directory`` returns."""
        if not content.strip():
            raise ResultStorageError("Execution source must not be blank.")
        body = content.encode("utf-8")
        checksum = sha256(body)
        relative = (
            self._paths.sources_relative(execution_id, operation_id)
            / f"{step_id}.py"
        )
        path = self._paths.resolve_relative(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_bytes()
            if existing != body:
                raise ResultStorageError(
                    "Execution source snapshot conflicts with existing content."
                )
        else:
            atomic_write(path, body, sync_directory=False)
        return ExecutionSourceReference(
            relative_path=relative.as_posix(),
            checksum_sha256=checksum,
            size_bytes=len(body),
        )

    def sync_directory(self, execution_id: UUID, operation_id: UUID) -> None:
        """An Operation's snapshots share one directory."""
        fsync_directory(
            self._paths.resolve_relative(
                self._paths.sources_relative(execution_id, operation_id)
            )
        )

    def read(self, reference: ExecutionSourceReference) -> str:
        path = self._paths.resolve_reference(reference.relative_path)
        body = path.read_bytes()
        if (
            len(body) != reference.size_bytes
            or sha256(body) != reference.checksum_sha256
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
