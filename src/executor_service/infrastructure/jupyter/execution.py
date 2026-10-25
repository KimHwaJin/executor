"""Code execution through the Jupyter extension's execution journals."""

import asyncio
import json
import time
from typing import Any
from uuid import UUID

import httpx
import websockets
from websockets.exceptions import (
    ConnectionClosedError,
    InvalidStatus,
    PayloadTooBig,
    WebSocketException,
)

from executor_service.domain.runtime import (
    RuntimeCatchUpHandler,
    RuntimeDriverError,
    RuntimeExecutionError,
    RuntimeExecutionResult,
    RuntimeOutputHandler,
    RuntimeOutputLimitExceededError,
    RuntimeSessionLostError,
)
from executor_service.infrastructure.jupyter.protocol import (
    as_output_record,
    error_summary,
    message_limit_closed,
)
from executor_service.infrastructure.jupyter.transport import (
    JupyterHttpTransport,
)

_RECONNECT_DELAY_SECONDS = 1.0
# Larger journal frames are parsed off the event loop so one big output does
# not stall heartbeats and the other Steps on this Pod.
_LARGE_MESSAGE_BYTES = 1024 * 1024


class _OutputDeliveryFailure(Exception):
    """Keep output storage exceptions outside transport exception mapping."""

    def __init__(self, original: Exception) -> None:
        super().__init__(type(original).__name__)
        self.original = original


class JupyterKernelExecutor:
    def __init__(
        self,
        transport: JupyterHttpTransport,
        max_output_message_bytes: int,
        *,
        reconnect_seconds: float = 30,
    ) -> None:
        self._transport = transport
        self._max_output_message_bytes = max_output_message_bytes
        self._reconnect_seconds = reconnect_seconds

    async def start(
        self, session_id: str, request_id: UUID, code: str
    ) -> None:
        # Jupyter may still send the code after a lost response, so never
        # give up on a transport error: resubmitting the same request ID is
        # safe and tells whether it was accepted.
        disconnected_at: float | None = None
        while True:
            try:
                response = await self._transport.request(
                    "POST",
                    f"/executor/kernels/{session_id}/executions",
                    json={"request_id": str(request_id), "code": code},
                    allowed_statuses={200, 201, 404},
                )
                break
            except RuntimeDriverError as exc:
                if not isinstance(exc.__cause__, httpx.RequestError):
                    raise
                disconnected_at = self._reconnect_allowed(disconnected_at, exc)
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
        if response.status_code == 404:
            raise RuntimeSessionLostError(
                "Jupyter kernel was not found when submitting code."
            )

    async def release(self, session_id: str, request_id: UUID) -> None:
        await self._transport.request(
            "DELETE",
            f"/executor/kernels/{session_id}/executions/{request_id}",
            allowed_statuses={204, 404},
        )

    async def follow(
        self,
        session_id: str,
        request_id: UUID,
        output_handler: RuntimeOutputHandler,
        on_caught_up: RuntimeCatchUpHandler | None = None,
    ) -> RuntimeExecutionResult:
        state = _JournalState()
        disconnected_at: float | None = None
        while True:
            try:
                async with websockets.connect(
                    self._transport.journal_uri(
                        session_id, request_id, state.seq
                    ),
                    additional_headers={
                        "Authorization": f"token {self._transport.token}"
                    },
                    max_size=self._max_output_message_bytes,
                    ping_interval=20,
                    ping_timeout=20,
                ) as websocket:
                    disconnected_at = None
                    async for raw in websocket:
                        result = await state.apply(
                            await _decode(raw), output_handler, on_caught_up
                        )
                        if result is not None:
                            return result
                raise ConnectionClosedError(None, None)
            except _OutputDeliveryFailure as exc:
                raise exc.original from exc.original.__cause__
            except InvalidStatus as exc:
                if exc.response.status_code == 404:
                    raise RuntimeSessionLostError(
                        "Jupyter has no record of this execution; it was "
                        "never received or the Jupyter server restarted."
                    ) from exc
                disconnected_at = self._reconnect_allowed(disconnected_at, exc)
            except PayloadTooBig as exc:
                raise RuntimeOutputLimitExceededError(
                    self._max_output_message_bytes
                ) from exc
            except (OSError, TimeoutError, WebSocketException) as exc:
                if message_limit_closed(exc):
                    raise RuntimeOutputLimitExceededError(
                        self._max_output_message_bytes
                    ) from exc
                disconnected_at = self._reconnect_allowed(disconnected_at, exc)
            # The journal keeps recording; reconnect after the last entry.
            await asyncio.sleep(_RECONNECT_DELAY_SECONDS)

    def _reconnect_allowed(
        self, disconnected_at: float | None, exc: BaseException
    ) -> float:
        now = time.monotonic()
        started = now if disconnected_at is None else disconnected_at
        if now - started >= self._reconnect_seconds:
            raise RuntimeDriverError(
                "Jupyter execution stream stayed unavailable for "
                f"{self._reconnect_seconds:.0f} seconds: "
                f"transport={type(exc).__name__}."
            ) from exc
        return started


class _JournalState:
    """Apply journal entries exactly once and in order."""

    def __init__(self) -> None:
        self.seq = 0
        self.execution_count: int | None = None
        self.error_message: str | None = None
        self.caught_up = False

    async def apply(
        self,
        entry: dict,
        output_handler: RuntimeOutputHandler,
        on_caught_up: RuntimeCatchUpHandler | None,
    ) -> RuntimeExecutionResult | None:
        entry_type = entry.get("type")
        if entry_type == "caught_up":
            if not self.caught_up:
                self.caught_up = True
                if on_caught_up is not None:
                    await on_caught_up(bool(entry.get("finished")))
            return None
        if entry.get("seq") != self.seq + 1:
            raise RuntimeDriverError("Jupyter execution journal has a gap.")
        self.seq += 1
        if entry_type == "terminal":
            if (
                entry.get("state") == "COMPLETED"
                and not self.caught_up
                and on_caught_up is not None
            ):
                # The replay reached the end: it finished before we joined.
                self.caught_up = True
                await on_caught_up(True)
            return self._finish(entry)
        msg_type = entry.get("msg_type")
        content = entry.get("content", {})
        if entry.get("channel") == "shell":
            if msg_type == "execute_reply":
                self.execution_count = content.get("execution_count")
                if content.get("status") == "error":
                    self.error_message = error_summary(content)
            return None
        record = as_output_record(msg_type, content)
        if record is None:
            return None
        if msg_type == "error":
            self.error_message = error_summary(content)
        try:
            await output_handler(record)
        except Exception as exc:
            raise _OutputDeliveryFailure(exc) from exc
        return None

    def _finish(self, entry: dict) -> RuntimeExecutionResult:
        state = entry.get("state")
        message = str(entry.get("message") or "")
        if state == "LOST":
            raise RuntimeSessionLostError(
                f"{message} The original execution cannot continue."
            )
        if state == "OUTPUT_LIMIT":
            raise RuntimeOutputLimitExceededError(
                kind="JOURNAL_SIZE", message=message
            )
        if state != "COMPLETED":
            raise RuntimeDriverError("Jupyter execution journal is invalid.")
        if self.error_message is not None:
            raise RuntimeExecutionError(self.error_message)
        return RuntimeExecutionResult(execution_count=self.execution_count)


async def _decode(raw: str | bytes) -> Any:
    if len(raw) > _LARGE_MESSAGE_BYTES:
        return await asyncio.to_thread(json.loads, raw)
    return json.loads(raw)
