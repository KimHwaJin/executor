"""Keep the upstream file response alive for the complete ASGI download."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, AsyncExitStack
from urllib.parse import quote

from anyio import CancelScope
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from executor_service.application.artifact_content import ArtifactContent


class ArtifactStreamingResponse(StreamingResponse):
    def __init__(
        self, content: ArtifactContent, resources: AsyncExitStack
    ) -> None:
        self._resources = resources
        byte_range = content.byte_range
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(byte_range.length),
            "Content-Disposition": (
                "attachment; filename*=UTF-8''" + quote(content.name, safe="")
            ),
            "ETag": f'"{content.checksum_sha256}"',
            "X-Checksum-SHA256": content.checksum_sha256,
            "Cache-Control": "no-store, no-transform",
        }
        if byte_range.partial:
            headers["Content-Range"] = (
                f"bytes {byte_range.start}-{byte_range.end}/{byte_range.size}"
            )
        super().__init__(
            content.body,
            status_code=206 if byte_range.partial else 200,
            media_type=content.media_type,
            headers=headers,
        )

    @classmethod
    async def open(
        cls, context: AbstractAsyncContextManager[ArtifactContent]
    ) -> ArtifactStreamingResponse:
        resources = AsyncExitStack()
        try:
            content = await resources.enter_async_context(context)
            return cls(content, resources)
        except BaseException:
            with CancelScope(shield=True):
                await resources.aclose()
            raise

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # BackgroundTask alone is insufficient: streaming/send exceptions
            # and client disconnects must release HTTP and Runtime resources too.
            with CancelScope(shield=True):
                await self._resources.aclose()
