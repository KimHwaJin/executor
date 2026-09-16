"""Bounded, unmasked body logging for failed REST Execution commands only."""

import json
import logging
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)


class ExecutionRequestErrorLoggingMiddleware:
    def __init__(
        self, app: ASGIApp, *, enabled: bool, max_body_bytes: int
    ) -> None:
        self.app = app
        self.enabled = enabled
        self.max_body_bytes = max_body_bytes

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        path = scope.get("path", "")
        if (
            not self.enabled
            or scope["type"] != "http"
            or scope.get("method") not in {"POST", "PUT", "PATCH", "DELETE"}
            or not (
                path == "/api/v1/executions"
                or path.startswith("/api/v1/executions/")
            )
        ):
            await self.app(scope, receive, send)
            return

        body = bytearray()
        error_body = bytearray()
        observed_bytes = 0
        body_complete = False
        response_status = 500
        logged = False

        async def capture_receive() -> Message:
            nonlocal observed_bytes, body_complete
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                observed_bytes += len(chunk)
                body.extend(chunk[: max(0, self.max_body_bytes - len(body))])
                body_complete = not message.get("more_body", False)
            return message

        def log_failure(exc: Exception | None = None) -> None:
            nonlocal logged
            if logged:
                return
            logged = True
            logger.error(
                "%s",
                json.dumps(
                    {
                        "event": "rest.execution.request_failed",
                        "request_id": str(uuid4()),
                        "method": scope["method"],
                        "path": path,
                        "status_code": response_status,
                        "request_body": body.decode("utf-8", errors="replace"),
                        "body_observed_bytes": observed_bytes,
                        "body_complete": body_complete,
                        "body_truncated": observed_bytes > len(body),
                        "error_response": error_body.decode(
                            "utf-8", errors="replace"
                        ),
                        "exception_type": type(exc).__name__ if exc else None,
                        "exception_message": str(exc) if exc else None,
                    },
                    ensure_ascii=False,
                ),
            )

        async def capture_send(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message["status"]
            elif (
                message["type"] == "http.response.body"
                and response_status >= 400
            ):
                error_body.extend(
                    message.get("body", b"")[: max(0, 16384 - len(error_body))]
                )
                if not message.get("more_body", False):
                    log_failure()
            await send(message)

        try:
            await self.app(scope, capture_receive, capture_send)
        except Exception as exc:
            # FastAPI's unhandled-exception response is generated outside user
            # middleware. Preserve the exception and its existing handler.
            log_failure(exc)
            raise
