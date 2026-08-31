"""Server-origin rate warnings must not masquerade as complete results."""

import copy
import json
from typing import Any

import pytest

from executor_service.domain.runtime import (
    RuntimeOutputLimitExceededError,
    RuntimeOutputLimitKind,
    RuntimeOutputRecord,
)
from executor_service.infrastructure._jupyter.output_limits import (
    server_output_limit,
)
from executor_service.infrastructure._jupyter.protocol import (
    deserialize_v1,
    serialize_v1,
)
from executor_service.infrastructure.jupyter import JupyterRuntimeDriver


def rate_warning(kind: RuntimeOutputLimitKind) -> str:
    name = "data" if kind == "DATA_RATE" else "message"
    setting = (
        "iopub_data_rate_limit"
        if kind == "DATA_RATE"
        else "iopub_msg_rate_limit"
    )
    return (
        f"IOPub {name} rate exceeded.\n"
        "The Jupyter server will temporarily stop sending output\n"
        "to the client in order to avoid crashing it.\n"
        "To change this limit, set the config variable\n"
        f"`--ServerApp.{setting}`.\n\n"
        "Current values:\n"
        f"ServerApp.{setting}=1000000.0 (bytes/sec)\n"
        "ServerApp.rate_limit_window=3.0 (secs)\n\n"
    )


def warning_message(kind: RuntimeOutputLimitKind) -> dict[str, Any]:
    return {
        "header": {"msg_type": "stream", "session": "connection-session"},
        "parent_header": {"msg_id": "request-id"},
        "metadata": {},
        "content": {"name": "stderr", "text": rate_warning(kind)},
    }


@pytest.mark.parametrize("kind", ["DATA_RATE", "MESSAGE_RATE"])
@pytest.mark.parametrize(
    "mismatch",
    [
        None,
        "kernel_session",
        "parent",
        "channel",
        "stdout",
        "type",
        "text_only",
        "no_header",
        "no_session",
        "not_warning",
    ],
)
def test_rate_warning_requires_matching_server_session_and_warning_shape(
    kind: RuntimeOutputLimitKind,
    mismatch: str | None,
) -> None:
    message = warning_message(kind)
    channel = "iopub"
    if mismatch == "kernel_session":
        message["header"]["session"] = "kernel-session"
    elif mismatch == "parent":
        message["parent_header"]["msg_id"] = "another-request"
    elif mismatch == "channel":
        channel = "shell"
    elif mismatch == "stdout":
        message["content"]["name"] = "stdout"
    elif mismatch == "type":
        message["header"]["msg_type"] = "display_data"
    elif mismatch == "text_only":
        message["content"]["text"] = "IOPub data rate exceeded."
    elif mismatch == "no_header":
        message["header"] = None
    elif mismatch == "no_session":
        message["header"].pop("session")
    elif mismatch == "not_warning":
        message["content"]["text"] = "a normal warning"
    observed = server_output_limit(
        channel,
        message,
        connection_session="connection-session",
        request_id="request-id",
    )
    assert observed == (kind if mismatch is None else None)


@pytest.mark.parametrize("kind", ["DATA_RATE", "MESSAGE_RATE"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize("server_origin", [False, True])
async def test_warning_is_saved_before_limit_error_but_kernel_echo_is_normal(
    monkeypatch: pytest.MonkeyPatch,
    kind: RuntimeOutputLimitKind,
    streaming: bool,
    binary: bool,
    server_origin: bool,
) -> None:
    class Socket:
        def __init__(self) -> None:
            self.messages: list[tuple[str, dict[str, Any]]] = []

        async def __aenter__(self) -> "Socket":
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def send(self, raw: bytes) -> None:
            _, request = deserialize_v1(raw)
            warning = copy.deepcopy(warning_message(kind))
            warning["header"]["session"] = (
                request["header"]["session"]
                if server_origin
                else "kernel-session"
            )
            warning["parent_header"]["msg_id"] = request["header"]["msg_id"]
            self.messages = [("iopub", warning)]
            for channel, msg_type, content in [
                (
                    "shell",
                    "execute_reply",
                    {"status": "ok", "execution_count": 1},
                ),
                ("iopub", "status", {"execution_state": "idle"}),
            ]:
                self.messages.append(
                    (
                        channel,
                        {
                            "header": {
                                "msg_type": msg_type,
                                "session": "kernel-session",
                            },
                            "parent_header": {
                                "msg_id": request["header"]["msg_id"]
                            },
                            "content": content,
                            "metadata": {},
                        },
                    )
                )

        async def recv(self) -> str | bytes:
            channel, message = self.messages.pop(0)
            return (
                serialize_v1(channel, message)
                if binary
                else json.dumps({"channel": channel, **message})
            )

    monkeypatch.setattr(
        "executor_service.infrastructure._jupyter.execution.websockets.connect",
        lambda *_args, **_kwargs: Socket(),
    )
    driver = JupyterRuntimeDriver("http://runtime.invalid", "test-only")
    outputs: list[RuntimeOutputRecord] = []

    async def collect(record: RuntimeOutputRecord) -> None:
        outputs.append(record)

    try:
        call = (
            driver.execute_streaming("kernel", "print('ok')", collect)
            if streaming
            else driver.execute("kernel", "print('ok')")
        )
        if server_origin:
            with pytest.raises(RuntimeOutputLimitExceededError) as raised:
                await call
            assert raised.value.kind == kind
            assert raised.value.max_message_bytes is None
            assert "incomplete" in str(raised.value)
            if not streaming:
                assert raised.value.outputs[0]["text"] == rate_warning(kind)
        else:
            result = await call
            assert result.execution_count == 1
            if not streaming:
                assert result.outputs[0]["text"] == rate_warning(kind)
        if streaming:
            assert len(outputs) == 1
            assert outputs[0].stream_name == "stderr"
            assert outputs[0].representations[0].content == rate_warning(kind)
    finally:
        await driver.close()
