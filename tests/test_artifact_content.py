import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from executor_resource_extension.file_download import (
    FileRangeError,
    parse_range,
)

from executor_service.application.artifact_content import (
    ArtifactContentService,
)
from executor_service.domain.enums import (
    ArtifactStatus,
    ArtifactStorageType,
    RuntimeType,
)
from executor_service.domain.errors import (
    ArtifactContentUnavailableError,
    ArtifactRangeNotSatisfiableError,
)
from executor_service.domain.runtime import (
    RuntimeByteRange,
    RuntimeFileContent,
    RuntimeFileRangeError,
)


class ArtifactQueries:
    def __init__(self, artifact: object, execution: object) -> None:
        self.artifact_value = artifact
        self.execution_value = execution

    async def artifact(self, artifact_id: UUID) -> object:
        return self.artifact_value

    async def execution(self, execution_id: UUID) -> object:
        return self.execution_value


class ArtifactStreamer:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls: list[tuple[RuntimeType, UUID | None, str, str | None]] = []
        self.closed = False

    @asynccontextmanager
    async def open_file(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
        range_header: str | None,
    ) -> AsyncIterator[RuntimeFileContent]:
        self.calls.append(
            (runtime_type, preferred_target_id, path, range_header)
        )
        content = self.content
        try:
            start, end, partial = parse_range(range_header, len(content))
        except FileRangeError as exc:
            raise RuntimeFileRangeError(exc.size) from exc

        async def body() -> AsyncIterator[bytes]:
            yield content[start : end + 1]

        try:
            yield RuntimeFileContent(
                RuntimeByteRange(start, end, len(content), partial),
                hashlib.sha256(content).hexdigest(),
                body(),
            )
        finally:
            self.closed = True


def _service(
    content: bytes = b"0123456789",
    *,
    storage_type: ArtifactStorageType = ArtifactStorageType.PV,
    status: ArtifactStatus = ArtifactStatus.AVAILABLE,
) -> tuple[ArtifactContentService, ArtifactStreamer, UUID]:
    target_id = uuid4()
    artifact = SimpleNamespace(
        execution_id=uuid4(),
        status=status,
        storage_type=storage_type,
        relative_path="users/u/artifacts/report.bin",
        size_bytes=len(content),
        checksum_sha256="a" * 64,
        name="report.bin",
        media_type="application/octet-stream",
    )
    execution = SimpleNamespace(
        runtime_type=RuntimeType.JUPYTER, runtime_target_id=target_id
    )
    streamer = ArtifactStreamer(content)
    queries = cast(Any, ArtifactQueries(artifact, execution))
    return ArtifactContentService(queries, streamer), streamer, target_id


async def _read(body: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in body])


async def test_artifact_content_streams_full_and_single_range() -> None:
    service, streamer, target_id = _service()
    async with service.open(uuid4(), None) as full:
        assert await _read(full.body) == b"0123456789"
        assert full.byte_range.partial is False
        assert not streamer.closed
    assert streamer.closed
    async with service.open(uuid4(), "bytes=2-5") as partial:
        assert await _read(partial.body) == b"2345"
        assert partial.byte_range.length == 4
    assert streamer.calls == [
        (RuntimeType.JUPYTER, target_id, "users/u/artifacts/report.bin", None),
        (
            RuntimeType.JUPYTER,
            target_id,
            "users/u/artifacts/report.bin",
            "bytes=2-5",
        ),
    ]


@pytest.mark.parametrize(
    "current", [b"", b"short", b"abcdefghij", b"longer" * 1000]
)
async def test_download_uses_current_file_not_registration_metadata(
    current: bytes,
) -> None:
    service, streamer, _ = _service()
    streamer.content = current
    async with service.open(uuid4(), None) as opened:
        assert opened.byte_range.length == len(current)
        assert opened.checksum_sha256 == hashlib.sha256(current).hexdigest()
        assert await _read(opened.body) == current


async def test_missing_observed_size_and_checksum_do_not_block_download() -> (
    None
):
    service, _, _ = _service()
    artifact = cast(Any, service)._queries.artifact_value
    artifact.size_bytes = artifact.checksum_sha256 = None
    async with service.open(uuid4(), None) as opened:
        assert await _read(opened.body) == b"0123456789"


async def test_range_is_validated_against_current_file_size() -> None:
    service, streamer, _ = _service()
    streamer.content = b"0" * 20
    async with service.open(uuid4(), "bytes=12-") as opened:
        assert opened.byte_range == RuntimeByteRange(12, 19, 20, True)
    streamer.content = b"0" * 3
    with pytest.raises(ArtifactRangeNotSatisfiableError):
        async with service.open(uuid4(), "bytes=5-"):
            raise AssertionError("A range beyond the current file must fail.")


async def test_empty_artifact_has_empty_full_body_and_rejects_range() -> None:
    service, streamer, _ = _service(b"")
    async with service.open(uuid4(), None) as content:
        assert content.byte_range.length == 0
        assert await _read(content.body) == b""
    assert streamer.closed
    with pytest.raises(ArtifactRangeNotSatisfiableError):
        async with service.open(uuid4(), "bytes=0-0"):
            raise AssertionError("An empty file has no satisfiable range.")


@pytest.mark.parametrize(
    "changes",
    [
        {"storage_type": ArtifactStorageType.S3},
        {"status": ArtifactStatus.DELETED},
        {"relative_path": "../secret"},
        {"relative_path": None},
    ],
)
async def test_rejects_unavailable_or_unsafe_artifacts(
    changes: dict[str, Any],
) -> None:
    service, streamer, _ = _service()
    artifact = cast(Any, service)._queries.artifact_value
    for key, value in changes.items():
        setattr(artifact, key, value)
    with pytest.raises(ArtifactContentUnavailableError):
        async with service.open(uuid4(), None):
            raise AssertionError(
                "Invalid Artifact must be rejected before Runtime access."
            )
    assert not streamer.calls
