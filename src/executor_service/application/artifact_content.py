"""Storage-neutral preparation of registered Artifact content downloads."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID

from executor_service.application.execution_queries import (
    ExecutionQueryService,
)
from executor_service.domain.enums import ArtifactStatus, ArtifactStorageType
from executor_service.domain.errors import (
    ArtifactContentUnavailableError,
    ArtifactRangeNotSatisfiableError,
)
from executor_service.domain.runtime import RuntimeArtifactContentAccess


@dataclass(frozen=True, slots=True)
class ArtifactByteRange:
    start: int
    end: int
    size: int
    partial: bool

    @property
    def length(self) -> int:
        return max(self.end - self.start + 1, 0)


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    name: str
    media_type: str
    checksum_sha256: str
    byte_range: ArtifactByteRange
    body: AsyncIterator[bytes]


class ArtifactContentService:
    def __init__(
        self,
        queries: ExecutionQueryService,
        runtime_storage: RuntimeArtifactContentAccess,
    ) -> None:
        self._queries = queries
        self._runtime_storage = runtime_storage

    async def open(
        self, artifact_id: UUID, range_header: str | None
    ) -> ArtifactContent:
        artifact = await self._queries.artifact(artifact_id)
        if artifact.status != ArtifactStatus.AVAILABLE:
            raise ArtifactContentUnavailableError(
                "Artifact content is available only for AVAILABLE Artifacts."
            )
        if artifact.storage_type != ArtifactStorageType.PV:
            raise ArtifactContentUnavailableError(
                "Artifact content download is not configured for this storage type."
            )
        if (
            artifact.relative_path is None
            or artifact.size_bytes is None
            or artifact.checksum_sha256 is None
        ):
            raise ArtifactContentUnavailableError(
                "Artifact storage metadata is incomplete."
            )
        _validate_relative_path(artifact.relative_path)
        execution = await self._queries.execution(artifact.execution_id)
        byte_range = parse_byte_range(range_header, artifact.size_bytes)
        return ArtifactContent(
            name=artifact.name,
            media_type=artifact.media_type or "application/octet-stream",
            checksum_sha256=artifact.checksum_sha256,
            byte_range=byte_range,
            body=(
                _empty_body()
                if artifact.size_bytes == 0
                else self._runtime_storage.stream_file(
                    execution.runtime_type,
                    execution.runtime_target_id,
                    artifact.relative_path,
                    byte_range.start,
                    byte_range.end,
                )
            ),
        )


def parse_byte_range(value: str | None, size: int) -> ArtifactByteRange:
    if size < 0:
        raise ValueError("Artifact size must not be negative.")
    if size == 0:
        if value is not None:
            raise _invalid_range(size)
        return ArtifactByteRange(start=0, end=-1, size=0, partial=False)
    if value is None:
        return ArtifactByteRange(
            start=0, end=size - 1, size=size, partial=False
        )
    if not value.startswith("bytes=") or "," in value:
        raise _invalid_range(size)
    raw = value.removeprefix("bytes=").strip()
    start_text, separator, end_text = raw.partition("-")
    if not separator:
        raise _invalid_range(size)
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix < 1:
                raise ValueError
            start = max(size - suffix, 0)
            end = size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
    except ValueError as exc:
        raise _invalid_range(size) from exc
    if start < 0 or start >= size or end < start:
        raise _invalid_range(size)
    end = min(end, size - 1)
    return ArtifactByteRange(
        start=start,
        end=end,
        size=size,
        partial=True,
    )


def _invalid_range(size: int) -> ArtifactRangeNotSatisfiableError:
    return ArtifactRangeNotSatisfiableError(
        "Requested Artifact byte range is not satisfiable.", size
    )


def _validate_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ArtifactContentUnavailableError(
            "Artifact storage path is not a safe relative path."
        )


async def _empty_body() -> AsyncIterator[bytes]:
    if False:
        yield b""
