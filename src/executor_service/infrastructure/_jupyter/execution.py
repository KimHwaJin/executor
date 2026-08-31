"""Jupyter kernel WebSocket code execution."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import websockets
from websockets.exceptions import (
    ConnectionClosedError,
    PayloadTooBig,
    WebSocketException,
)
from websockets.typing import Subprotocol

from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeExecutionError,
    RuntimeExecutionResult,
    RuntimeOutputHandler,
    RuntimeOutputLimitExceededError,
)
from executor_service.infrastructure._jupyter.output_limits import (
    server_output_limit,
)
from executor_service.infrastructure._jupyter.protocol import (
    as_notebook_output,
    as_output_record,
    deserialize_v1,
    error_summary,
    message_limit_closed,
    serialize_v1,
)
from executor_service.infrastructure._jupyter.transport import (
    JupyterHttpTransport,
)


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
    ) -> None:
        self._transport = transport
        self._max_output_message_bytes = max_output_message_bytes

    async def execute(
        self, session_id: str, code: str
    ) -> RuntimeExecutionResult:
        return await self._execute(session_id, code, output_handler=None)

    async def execute_streaming(
        self,
        session_id: str,
        code: str,
        output_handler: RuntimeOutputHandler,
    ) -> RuntimeExecutionResult:
        return await self._execute(
            session_id,
            code,
            output_handler=output_handler,
        )

    async def _execute(
        self,
        session_id: str,
        code: str,
        *,
        output_handler: RuntimeOutputHandler | None,
    ) -> RuntimeExecutionResult:
        websocket_session_id = str(uuid4())
        message_id = str(uuid4())
        uri = self._transport.channels_uri(
            session_id,
            websocket_session_id,
        )
        request = {
            "header": {
                "msg_id": message_id,
                "username": "executor",
                "session": websocket_session_id,
                "date": datetime.now(UTC).isoformat(),
                "msg_type": "execute_request",
                "version": "5.3",
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code,
                "silent": False,
                "store_history": True,
                "user_expressions": {},
                "allow_stdin": False,
                "stop_on_error": True,
            },
        }
        outputs: list[dict[str, Any]] = []
        execution_count: int | None = None
        reply_received = False
        idle_received = False
        error_message: str | None = None

        try:
            async with websockets.connect(
                uri,
                subprotocols=[Subprotocol("v1.kernel.websocket.jupyter.org")],
                additional_headers={
                    "Authorization": f"token {self._transport.token}"
                },
                max_size=self._max_output_message_bytes,
                ping_interval=20,
                ping_timeout=20,
            ) as websocket:
                await websocket.send(serialize_v1("shell", request))
                while not (reply_received and idle_received):
                    raw = await websocket.recv()
                    channel, message = deserialize_v1(raw)
                    parent_id = message.get("parent_header", {}).get("msg_id")
                    if parent_id != message_id:
                        continue
                    msg_type = message.get("header", {}).get("msg_type")
                    content = message.get("content", {})
                    if channel == "shell" and msg_type == "execute_reply":
                        reply_received = True
                        execution_count = content.get("execution_count")
                        if content.get("status") == "error":
                            error_message = error_summary(content)
                    elif channel == "iopub" and msg_type == "status":
                        idle_received = (
                            content.get("execution_state") == "idle"
                        )
                    elif channel == "iopub":
                        output = as_notebook_output(msg_type, content)
                        if output is not None:
                            if output_handler is None:
                                outputs.append(output)
                            else:
                                record = as_output_record(msg_type, content)
                                if record is None:
                                    raise RuntimeDriverError(
                                        "Jupyter output mapping is incomplete."
                                    )
                                try:
                                    await output_handler(record)
                                except Exception as exc:
                                    raise _OutputDeliveryFailure(exc) from exc
                            if output["output_type"] == "error":
                                error_message = error_summary(output)
                            limit_kind = server_output_limit(
                                channel,
                                message,
                                connection_session=websocket_session_id,
                                request_id=message_id,
                            )
                            if limit_kind is not None:
                                # Preserve the warning as evidence, then use
                                # the existing interrupt/confirm workflow.
                                raise RuntimeOutputLimitExceededError(
                                    kind=limit_kind,
                                    outputs=outputs,
                                )
        except _OutputDeliveryFailure as exc:
            raise exc.original from exc.original.__cause__
        except PayloadTooBig as exc:
            raise RuntimeOutputLimitExceededError(
                self._max_output_message_bytes, outputs=outputs
            ) from exc
        except ConnectionClosedError as exc:
            if message_limit_closed(exc):
                raise RuntimeOutputLimitExceededError(
                    self._max_output_message_bytes, outputs=outputs
                ) from exc
            raise RuntimeDriverError(
                "Jupyter kernel channel became unavailable: "
                f"received_close_code={exc.rcvd.code if exc.rcvd else None}, "
                f"sent_close_code={exc.sent.code if exc.sent else None}."
            ) from exc
        except (OSError, TimeoutError, WebSocketException) as exc:
            raise RuntimeDriverError(
                "Jupyter kernel channel became unavailable: "
                f"transport={type(exc).__name__}."
            ) from exc

        if error_message is not None:
            raise RuntimeExecutionError(error_message, outputs)
        return RuntimeExecutionResult(
            outputs=outputs,
            execution_count=execution_count,
        )
