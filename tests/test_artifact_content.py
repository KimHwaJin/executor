from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from executor_service.application.artifact_content import (
    ArtifactContentService,
    parse_byte_range,
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


class ArtifactQueries:
    def __init__(self, artifact: object, execution: object) -> None:
        self.artifact_value = artifact
        self.execution_value = execution

    async def artifact(self, artifact_id: UUID) -> object:
        del artifact_id
        return self.artifact_value

    async def execution(self, execution_id: UUID) -> object:
        del execution_id
        return self.execution_value


class ArtifactStreamer:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.calls: list[tuple[RuntimeType, UUID | None, str, int, int]] = []

    async def stream_file(
        self,
        runtime_type: RuntimeType,
        preferred_target_id: UUID | None,
        path: str,
        start: int,
        end: int,
    ) -> AsyncIterator[bytes]:
        self.calls.append(
            (runtime_type, preferred_target_id, path, start, end)
        )
        yield self.content[start : end + 1]


def _service(
    content: bytes = b"0123456789",
    *,
    storage_type: ArtifactStorageType = ArtifactStorageType.PV,
    status: ArtifactStatus = ArtifactStatus.AVAILABLE,
) -> tuple[ArtifactContentService, ArtifactStreamer, UUID]:
    execution_id = uuid4()
    target_id = uuid4()
    artifact = SimpleNamespace(
        execution_id=execution_id,
        status=status,
        storage_type=storage_type,
        relative_path="users/u/artifacts/report.bin",
        size_bytes=len(content),
        checksum_sha256="a" * 64,
        name="report.bin",
        media_type="application/octet-stream",
    )
    execution = SimpleNamespace(
        runtime_type=RuntimeType.JUPYTER,
        runtime_target_id=target_id,
    )
    streamer = ArtifactStreamer(content)
    queries = cast(Any, ArtifactQueries(artifact, execution))
    return ArtifactContentService(queries, streamer), streamer, target_id


async def _read(body: AsyncIterator[bytes]) -> bytes:
    chunks = [chunk async for chunk in body]
    return b"".join(chunks)


async def test_artifact_content_streams_full_and_single_range() -> None:
    service, streamer, target_id = _service()

    full = await service.open(uuid4(), None)
    partial = await service.open(uuid4(), "bytes=2-5")

    assert await _read(full.body) == b"0123456789"
    assert full.byte_range.partial is False
    assert await _read(partial.body) == b"2345"
    assert partial.byte_range.length == 4
    assert streamer.calls == [
        (
            RuntimeType.JUPYTER,
            target_id,
            "users/u/artifacts/report.bin",
            0,
            9,
        ),
        (
            RuntimeType.JUPYTER,
            target_id,
            "users/u/artifacts/report.bin",
            2,
            5,
        ),
    ]


async def test_empty_artifact_has_an_empty_full_body_and_rejects_range() -> (
    None
):
    service, streamer, _ = _service(b"")

    content = await service.open(uuid4(), None)

    assert content.byte_range.length == 0
    assert await _read(content.body) == b""
    assert streamer.calls == []
    with pytest.raises(ArtifactRangeNotSatisfiableError):
        await service.open(uuid4(), "bytes=0-0")


def test_artifact_range_supports_open_and_suffix_ranges() -> None:
    assert parse_byte_range("bytes=7-", 10).start == 7
    suffix = parse_byte_range("bytes=-3", 10)
    assert (suffix.start, suffix.end) == (7, 9)
    with pytest.raises(ArtifactRangeNotSatisfiableError):
        parse_byte_range("bytes=0-1,4-5", 10)
    with pytest.raises(ArtifactRangeNotSatisfiableError):
        parse_byte_range("bytes=10-", 10)


async def test_artifact_content_rejects_unsupported_or_unavailable_storage() -> (
    None
):
    s3_service, _, _ = _service(storage_type=ArtifactStorageType.S3)
    deleted_service, _, _ = _service(status=ArtifactStatus.DELETED)

    with pytest.raises(ArtifactContentUnavailableError):
        await s3_service.open(uuid4(), None)
    with pytest.raises(ArtifactContentUnavailableError):
        await deleted_service.open(uuid4(), None)


async def test_artifact_content_rejects_unsafe_persisted_path() -> None:
    service, _, _ = _service()
    queries = cast(Any, service)._queries
    queries.artifact_value.relative_path = "../secret"

    with pytest.raises(ArtifactContentUnavailableError):
        await service.open(uuid4(), None)
