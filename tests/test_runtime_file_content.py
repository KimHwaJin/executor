import asyncio
import hashlib
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

from executor_service.domain.enums import RuntimeType
from executor_service.domain.runtime import (
    RuntimeByteRange,
    RuntimeDriverError,
    RuntimeFileContent,
    RuntimeFileRangeError,
    RuntimeFileUnavailableError,
)
from executor_service.infrastructure._jupyter.file_content import (
    open_file_response,
)
from executor_service.infrastructure.runtime_storage import (
    FleetRuntimeStorageAccess,
)


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, chunks: Sequence[bytes | Exception]) -> None:
        self.chunks = chunks
        self.closed = False
        self.reads = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.reads += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("early_close", [False, True])
async def test_stream_is_not_buffered_and_closes(early_close: bool) -> None:
    stream = TrackedStream([b"abc", b"def"])
    checksum = hashlib.sha256(b"abcdef").hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"path": "reports/file.bin"}
        assert request.headers["Range"] == "bytes=0-5"
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            206,
            headers={
                "Content-Length": "6",
                "Content-Range": "bytes 0-5/6",
                "X-Checksum-SHA256": checksum,
                "ETag": f'"{checksum}"',
            },
            stream=stream,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runtime"
    ) as client:
        async with open_file_response(
            client, "reports/file.bin", "bytes=0-5", 30
        ) as opened:
            assert stream.reads == 0
            assert not stream.closed
            assert opened.byte_range == RuntimeByteRange(0, 5, 6, True)
            if not early_close:
                assert (
                    b"".join([chunk async for chunk in opened.body])
                    == b"abcdef"
                )
        assert stream.closed


@pytest.mark.parametrize(
    ("status", "headers", "error"),
    [
        (416, {"Content-Range": "bytes */3"}, RuntimeFileRangeError),
        (404, {}, RuntimeFileUnavailableError),
        (409, {}, RuntimeFileUnavailableError),
        (200, {"Content-Length": "3"}, RuntimeDriverError),
        (
            206,
            {"Content-Length": "3", "Content-Range": "bytes 1-9/4"},
            RuntimeDriverError,
        ),
    ],
)
async def test_invalid_download_fails_before_body_and_closes(
    status: int, headers: dict[str, str], error: type[Exception]
) -> None:
    stream = TrackedStream([b"bad"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, headers=headers, stream=stream)
        ),
        base_url="http://runtime",
    ) as client:
        with pytest.raises(error):
            async with open_file_response(client, "file.bin", None, 30):
                raise AssertionError(
                    "Invalid metadata must fail before exposing the stream."
                )
    assert stream.closed
    assert stream.reads == 0


@pytest.mark.parametrize("chunks", [[b"ab"], [b"abcdefg"]])
async def test_incorrect_body_length_is_not_a_success(
    chunks: list[bytes],
) -> None:
    stream = TrackedStream(chunks)
    checksum = hashlib.sha256(b"abc").hexdigest()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={
                    "Content-Length": "3",
                    "X-Checksum-SHA256": checksum,
                    "ETag": f'"{checksum}"',
                },
                stream=stream,
            )
        ),
        base_url="http://runtime",
    ) as client:
        with pytest.raises(RuntimeDriverError):
            async with open_file_response(
                client, "file.bin", None, 30
            ) as opened:
                _ = [chunk async for chunk in opened.body]
    assert stream.closed


async def test_stream_transport_error_is_sanitized_and_closes() -> None:
    stream = TrackedStream([b"a", httpx.ReadError("upstream details")])
    checksum = hashlib.sha256(b"abc").hexdigest()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                headers={
                    "Content-Length": "3",
                    "X-Checksum-SHA256": checksum,
                    "ETag": f'"{checksum}"',
                },
                stream=stream,
            )
        ),
        base_url="http://runtime",
    ) as client:
        with pytest.raises(RuntimeDriverError, match="received=1, expected=3"):
            async with open_file_response(
                client, "file.bin", None, 30
            ) as opened:
                _ = [chunk async for chunk in opened.body]
    assert stream.closed


class FileDriver:
    def __init__(
        self, setup_error: Exception | None = None, body_error: bool = False
    ) -> None:
        self.setup_error = setup_error
        self.body_error = body_error
        self.closed = False
        self.file_closed = False

    @asynccontextmanager
    async def open_file(
        self, path: str, range_header: str | None
    ) -> AsyncIterator[RuntimeFileContent]:
        if self.setup_error:
            raise self.setup_error

        async def body() -> AsyncIterator[bytes]:
            if self.body_error:
                raise RuntimeDriverError("stream disconnected")
            yield b"ok"

        try:
            yield RuntimeFileContent(
                RuntimeByteRange(0, 1, 2, False), "a" * 64, body()
            )
        finally:
            self.file_closed = True

    async def close(self) -> None:
        self.closed = True


def fleet(drivers: list[FileDriver]) -> FleetRuntimeStorageAccess:
    class Registry:
        def resolve_credential(self, *_: object) -> str:
            return "test-only"

    class Factory:
        def create(
            self, _kind: object, config: dict[str, int], _token: str
        ) -> FileDriver:
            return drivers[config["index"]]

    class Fleet(FleetRuntimeStorageAccess):
        async def _candidates(
            self, runtime_type: RuntimeType, preferred_target_id: UUID | None
        ) -> Any:
            return [
                SimpleNamespace(
                    id=uuid4(),
                    runtime_type=RuntimeType.JUPYTER,
                    credential_ref=None,
                    credential_ciphertext=None,
                    connection_config={"index": index},
                )
                for index in range(len(drivers))
            ]

    return Fleet(cast(Any, None), cast(Any, Registry()), cast(Any, Factory()))


async def test_fleet_falls_back_only_during_setup() -> None:
    drivers = [FileDriver(RuntimeDriverError("offline")), FileDriver()]
    async with fleet(drivers).open_file(
        RuntimeType.JUPYTER, None, "file", None
    ) as opened:
        assert drivers[0].closed
        assert not drivers[1].closed
        assert b"".join([chunk async for chunk in opened.body]) == b"ok"
    assert drivers[1].closed and drivers[1].file_closed


async def test_no_fallback_after_metadata_even_before_first_body_byte() -> (
    None
):
    drivers = [FileDriver(body_error=True), FileDriver()]
    with pytest.raises(RuntimeDriverError, match="disconnected"):
        async with fleet(drivers).open_file(
            RuntimeType.JUPYTER, None, "file", None
        ) as opened:
            _ = [chunk async for chunk in opened.body]
    assert drivers[0].closed and drivers[0].file_closed
    assert not drivers[1].closed and not drivers[1].file_closed


@pytest.mark.parametrize(
    "error", [RuntimeFileRangeError(5), RuntimeFileUnavailableError("missing")]
)
async def test_fleet_does_not_retry_file_errors(error: Exception) -> None:
    drivers = [FileDriver(error), FileDriver()]
    with pytest.raises(type(error)):
        async with fleet(drivers).open_file(
            RuntimeType.JUPYTER, None, "file", None
        ):
            raise AssertionError("Request must fail.")
    assert drivers[0].closed and not drivers[1].closed


async def test_cancellation_releases_fleet_download() -> None:
    driver = FileDriver()
    ready = asyncio.Event()

    async def download() -> None:
        async with fleet([driver]).open_file(
            RuntimeType.JUPYTER, None, "file", None
        ):
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(download())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert driver.file_closed and driver.closed
