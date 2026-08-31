import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from starlette.requests import ClientDisconnect
from starlette.types import Message

from executor_service.application.artifact_content import ArtifactContent
from executor_service.domain.runtime import RuntimeByteRange
from executor_service.interfaces.http._executions.file_response import (
    ArtifactStreamingResponse,
)


@pytest.mark.parametrize("failure", [None, "body", "send", "cancel"])
async def test_download_context_lives_until_asgi_response_finishes(
    failure: str | None,
) -> None:
    closed = False

    async def body() -> AsyncIterator[bytes]:
        assert not closed
        yield b"abc"
        if failure == "body":
            raise RuntimeError("read interrupted")
        yield b"def"

    @asynccontextmanager
    async def opened() -> AsyncIterator[ArtifactContent]:
        nonlocal closed
        try:
            yield ArtifactContent(
                "file.bin",
                "application/octet-stream",
                "a" * 64,
                RuntimeByteRange(0, 5, 6, False),
                body(),
            )
        finally:
            await asyncio.sleep(0)
            closed = True

    sent: list[Message] = []

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            if failure == "send":
                raise OSError("client disconnected")
            if failure == "cancel":
                raise asyncio.CancelledError
        sent.append(message)

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    response = await ArtifactStreamingResponse.open(opened())
    assert not closed
    try:
        if failure:
            expected_error = {
                "body": RuntimeError,
                "send": ClientDisconnect,
                "cancel": asyncio.CancelledError,
            }[failure]
            with pytest.raises(expected_error):
                await response(
                    {"type": "http", "asgi": {"spec_version": "2.4"}},
                    receive,
                    send,
                )
        else:
            await response(
                {"type": "http", "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
            assert (
                b"".join(item.get("body", b"") for item in sent) == b"abcdef"
            )
    finally:
        assert closed
