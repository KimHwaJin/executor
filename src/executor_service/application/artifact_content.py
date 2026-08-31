"""Storage-neutral preparation of registered Artifact content downloads."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from executor_service.domain.runtime import (
    RuntimeArtifactContentAccess,
    RuntimeByteRange,
    RuntimeFileRangeError,
    RuntimeFileUnavailableError,
)


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    name: str
    media_type: str
    checksum_sha256: str
    byte_range: RuntimeByteRange
    body: AsyncIterator[bytes]


class ArtifactContentService:
    def __init__(
        self,
        queries: ExecutionQueryService,
        runtime_storage: RuntimeArtifactContentAccess,
    ) -> None:
        self._queries = queries
        self._runtime_storage = runtime_storage

    @asynccontextmanager
    async def open(
        self, artifact_id: UUID, range_header: str | None
    ) -> AsyncIterator[ArtifactContent]:
        artifact = await self._queries.artifact(artifact_id)
        if artifact.status != ArtifactStatus.AVAILABLE:
            raise ArtifactContentUnavailableError(
                "Artifact content is available only for AVAILABLE Artifacts."
            )
        if artifact.storage_type != ArtifactStorageType.PV:
            raise ArtifactContentUnavailableError(
                "Artifact content download is not configured for this storage type."
            )
        if artifact.relative_path is None:
            raise ArtifactContentUnavailableError(
                "Artifact storage path is missing."
            )
        _validate_relative_path(artifact.relative_path)
        execution = await self._queries.execution(artifact.execution_id)
        try:
            async with self._runtime_storage.open_file(
                execution.runtime_type,
                execution.runtime_target_id,
                artifact.relative_path,
                range_header,
            ) as opened:
                # Registration metadata is an observation, not the current
                # file's read boundary or its download checksum.
                yield ArtifactContent(
                    name=artifact.name,
                    media_type=artifact.media_type
                    or "application/octet-stream",
                    checksum_sha256=opened.checksum_sha256,
                    byte_range=opened.byte_range,
                    body=opened.body,
                )
        except RuntimeFileRangeError as exc:
            raise ArtifactRangeNotSatisfiableError(str(exc), exc.size) from exc
        except RuntimeFileUnavailableError as exc:
            raise ArtifactContentUnavailableError(str(exc)) from exc


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
