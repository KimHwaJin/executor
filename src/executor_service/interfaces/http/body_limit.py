"""Reject request bodies above a fixed size before any handler reads them."""

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from executor_service.domain.errors import ErrorCode, ErrorIssue
from executor_service.interfaces.error_body import error_content


class _BodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """413 for a body over ``max_bytes``, by header or by counted bytes.

    Inline Step code is the largest legitimate input; without a cap one
    request could hold hundreds of megabytes in an API Pod.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.max_bytes:
                    await self._reject(send)
                    return
        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if response_started:
                raise
            await self._reject(send)

    async def _reject(self, send: Send) -> None:
        message = f"Request body exceeds {self.max_bytes} bytes."
        body = json.dumps(
            error_content(
                ErrorCode.REQUEST_BODY_TOO_LARGE,
                message,
                (
                    ErrorIssue(
                        ("body",),
                        message,
                        "body_too_large",
                        {"limit_bytes": self.max_bytes},
                    ),
                ),
            )
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
