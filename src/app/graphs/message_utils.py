"""Agent Chat-compatible message content helpers."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4


def as_message_content(value: Any) -> str:
    """Return content in the string form expected by Agent Chat."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def append_messages_with_ids(
    current: list[Any] | None,
    updates: list[Any] | None,
) -> list[Any]:
    """Append messages with Agent Chat-compatible IDs and message types."""
    messages = []
    for message in [*(current or []), *(updates or [])]:
        if isinstance(message, dict):
            message = dict(message)
            if not message.get("id"):
                message["id"] = str(uuid4())
            if not message.get("type"):
                role = message.get("role")
                if role == "assistant":
                    message["type"] = "ai"
                elif role == "user":
                    message["type"] = "human"
        elif hasattr(message, "model_copy") and not getattr(message, "id", None):
            message = message.model_copy(update={"id": str(uuid4())})
        messages.append(message)
    return messages


__all__ = ["append_messages_with_ids", "as_message_content"]
