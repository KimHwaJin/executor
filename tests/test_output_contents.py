import hashlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from executor_service.application.output_contents import (
    ExecutionOutputContentService,
)
from executor_service.domain.enums import RuntimeType
from executor_service.domain.runtime import RuntimeOutputContentChunk

EXECUTION_ID = UUID("11111111-1111-4111-8111-111111111111")
OPERATION_ID = UUID("22222222-2222-4222-8222-222222222222")
STEP_ID = UUID("33333333-3333-4333-8333-333333333333")
ATTEMPT_ID = UUID("44444444-4444-4444-8444-444444444444")
TARGET_ID = UUID("55555555-5555-4555-8555-555555555555")
JOURNAL_ID = UUID("66666666-6666-4666-8666-666666666666")
OUTPUT_ID = UUID("77777777-7777-4777-8777-777777777777")
REPRESENTATION_ID = UUID("88888888-8888-4888-8888-888888888888")


class _Queries:
    def __init__(self, body: bytes, media_type: str) -> None:
        checksum = hashlib.sha256(body).hexdigest()
        self.execution_view = SimpleNamespace(
            workspace_path="users/u/executions/e",
            runtime_type=RuntimeType.JUPYTER,
        )
        self.output_view = SimpleNamespace(
            operation_id=OPERATION_ID,
            execution_step_id=STEP_ID,
            execution_attempt_id=ATTEMPT_ID,
            sequence=0,
            fencing_token=9,
            runtime_target_id=TARGET_ID,
            runtime_session_id="kernel-1",
            journal_id=JOURNAL_ID,
            representations=(
                SimpleNamespace(
                    id=REPRESENTATION_ID,
                    media_type=media_type,
                    size_bytes=len(body),
                    checksum_sha256=checksum,
                    complete=True,
                ),
            ),
        )

    async def execution(self, execution_id: UUID) -> Any:
        assert execution_id == EXECUTION_ID
        return self.execution_view

    async def output(self, execution_id: UUID, output_id: UUID) -> Any:
        assert execution_id == EXECUTION_ID
        assert output_id == OUTPUT_ID
        return self.output_view


class _ContentStorage:
    def __init__(self, body: bytes, media_type: str) -> None:
        self.body = body
        self.media_type = media_type
        self.checksum = hashlib.sha256(body).hexdigest()
        self.ranges: list[tuple[int, int]] = []

    async def read_output_content(
        self, *args: Any, start: int, end_exclusive: int, **kwargs: Any
    ) -> RuntimeOutputContentChunk:
        del args, kwargs
        self.ranges.append((start, end_exclusive))
        return RuntimeOutputContentChunk(
            content=self.body[start:end_exclusive],
            media_type=self.media_type,
            size_bytes=len(self.body),
            checksum_sha256=self.checksum,
            complete=True,
            start=start,
            end_exclusive=end_exclusive,
        )

    async def stream_output_content(
        self, *args: Any, start: int, end_exclusive: int, **kwargs: Any
    ) -> AsyncIterator[bytes]:
        del args, kwargs
        self.ranges.append((start, end_exclusive))
        midpoint = min(start + 4, end_exclusive)
        yield self.body[start:midpoint]
        if midpoint < end_exclusive:
            yield self.body[midpoint:end_exclusive]


async def test_streams_verified_output_content_in_bounded_chunks() -> None:
    body = b"0123456789"
    storage = _ContentStorage(body, "text/plain")
    service = ExecutionOutputContentService(
        cast(Any, _Queries(body, "text/plain")),
        storage,
    )
    descriptor = await service.describe(
        EXECUTION_ID, OUTPUT_ID, REPRESENTATION_ID
    )

    streamed = b"".join(
        [chunk async for chunk in service.stream(descriptor, 1, 9)]
    )

    assert streamed == b"12345678"
    assert storage.ranges == [(1, 9)]


async def test_inlines_only_small_utf8_text() -> None:
    body = "분석 결과".encode()
    service = ExecutionOutputContentService(
        cast(Any, _Queries(body, "text/plain")),
        _ContentStorage(body, "text/plain"),
    )
    descriptor = await service.describe(
        EXECUTION_ID, OUTPUT_ID, REPRESENTATION_ID
    )

    assert (
        await service.read_inline_text(descriptor, max_bytes=1024)
        == "분석 결과"
    )
    assert await service.read_inline_text(descriptor, max_bytes=1) is None
