"""Recognize Jupyter-generated rate warnings, not arbitrary kernel stderr.

Jupyter Server sets its connection Session to the `session_id` query parameter.
Its write_stderr() uses that Session when the rate limiter suppresses messages.
Ordinary kernel output instead carries the kernel's own header.session. This
is a protocol compatibility check, not an authentication/sandbox boundary.
"""

from typing import Any

from executor_service.domain.runtime import RuntimeOutputLimitKind


def server_output_limit(
    channel: str,
    message: dict[str, Any],
    *,
    connection_session: str,
    request_id: str,
) -> RuntimeOutputLimitKind | None:
    header = message.get("header", {})
    parent = message.get("parent_header", {})
    content = message.get("content", {})
    if (
        channel != "iopub"
        or not isinstance(header, dict)
        or not isinstance(parent, dict)
        or not isinstance(content, dict)
        or header.get("session") != connection_session
        or header.get("msg_type") != "stream"
        or parent.get("msg_id") != request_id
        or content.get("name") != "stderr"
    ):
        return None
    text = content.get("text")
    if not isinstance(text, str):
        return None
    limits: tuple[tuple[str, RuntimeOutputLimitKind, str], ...] = (
        ("data", "DATA_RATE", "iopub_data_rate_limit"),
        ("message", "MESSAGE_RATE", "iopub_msg_rate_limit"),
    )
    for name, kind, setting in limits:
        if (
            text.startswith(
                f"IOPub {name} rate exceeded.\n"
                "The Jupyter server will temporarily stop sending output\n"
            )
            and f"ServerApp.{setting}" in text
        ):
            return kind
    return None
