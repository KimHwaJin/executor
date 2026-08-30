"""Jupyter kernel WebSocket protocol and output conversion."""

import json
from typing import Any

from websockets.exceptions import PayloadTooBig

from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeOutputRecord,
    RuntimeOutputRepresentation,
)


def serialize_v1(channel: str, message: dict[str, Any]) -> bytes:
    parts = [
        json.dumps(message[key], separators=(",", ":")).encode()
        for key in ("header", "parent_header", "metadata", "content")
    ]
    channel_bytes = channel.encode()
    offsets = [8 * (1 + 1 + len(parts) + 1)]
    offsets.append(offsets[-1] + len(channel_bytes))
    for part in parts:
        offsets.append(offsets[-1] + len(part))
    return b"".join(
        [
            len(offsets).to_bytes(8, "little"),
            *(offset.to_bytes(8, "little") for offset in offsets),
            channel_bytes,
            *parts,
        ]
    )


def deserialize_v1(raw: str | bytes) -> tuple[str, dict[str, Any]]:
    if isinstance(raw, str):
        message = json.loads(raw)
        return str(message.get("channel", "")), message
    offset_count = int.from_bytes(raw[:8], "little")
    offsets = [
        int.from_bytes(raw[8 * (index + 1) : 8 * (index + 2)], "little")
        for index in range(offset_count)
    ]
    channel = raw[offsets[0] : offsets[1]].decode()
    parts = [raw[offsets[index] : offsets[index + 1]] for index in range(1, 5)]
    header, parent_header, metadata, content = (
        json.loads(part) for part in parts
    )
    return channel, {
        "header": header,
        "parent_header": parent_header,
        "metadata": metadata,
        "content": content,
    }


def as_notebook_output(
    msg_type: str | None, content: dict[str, Any]
) -> dict[str, Any] | None:
    if msg_type == "stream":
        return {
            "output_type": "stream",
            "name": content["name"],
            "text": content["text"],
        }
    if msg_type in {"display_data", "execute_result"}:
        output = {
            "output_type": msg_type,
            "data": content.get("data", {}),
            "metadata": content.get("metadata", {}),
        }
        if msg_type == "execute_result":
            output["execution_count"] = content.get("execution_count")
        return output
    if msg_type == "error":
        return {
            "output_type": "error",
            "ename": content.get("ename", "Error"),
            "evalue": content.get("evalue", ""),
            "traceback": content.get("traceback", []),
        }
    return None


def as_output_record(
    msg_type: str | None, content: dict[str, Any]
) -> RuntimeOutputRecord | None:
    if msg_type == "stream":
        return RuntimeOutputRecord(
            kind="STREAM",
            stream_name=str(content.get("name", "stdout")),
            representations=(
                RuntimeOutputRepresentation(
                    media_type="text/plain",
                    encoding="UTF8",
                    content=str(content.get("text", "")),
                ),
            ),
        )
    if msg_type in {"display_data", "execute_result"}:
        data = content.get("data", {})
        if not isinstance(data, dict):
            raise RuntimeDriverError("Jupyter display data is invalid.")
        representations = tuple(
            output_representation(str(media_type), value)
            for media_type, value in data.items()
        )
        if not representations:
            representations = (
                RuntimeOutputRepresentation(
                    media_type="application/json",
                    encoding="UTF8",
                    content="{}",
                ),
            )
        metadata = content.get("metadata", {})
        if not isinstance(metadata, dict):
            raise RuntimeDriverError("Jupyter output metadata is invalid.")
        transient = content.get("transient")
        record_metadata = dict(metadata)
        if isinstance(transient, dict) and transient:
            record_metadata["transient"] = transient
        execution_count = content.get("execution_count")
        if execution_count is not None and type(execution_count) is not int:
            raise RuntimeDriverError("Jupyter execution_count is invalid.")
        return RuntimeOutputRecord(
            kind="RESULT" if msg_type == "execute_result" else "DISPLAY",
            execution_count=execution_count,
            representations=representations,
            metadata=record_metadata,
        )
    if msg_type == "error":
        name = str(content.get("ename", "Error"))
        value = str(content.get("evalue", ""))
        traceback = content.get("traceback", [])
        if not isinstance(traceback, list):
            raise RuntimeDriverError("Jupyter traceback is invalid.")
        text = "\n".join(str(line) for line in traceback)
        if not text:
            text = f"{name}: {value}"
        return RuntimeOutputRecord(
            kind="ERROR",
            representations=(
                RuntimeOutputRepresentation(
                    media_type="text/plain",
                    encoding="UTF8",
                    content=text,
                ),
            ),
            metadata={"ename": name, "evalue": value},
        )
    return None


def output_representation(
    media_type: str, value: Any
) -> RuntimeOutputRepresentation:
    normalized_media_type = media_type.strip().lower()
    if not normalized_media_type or "/" not in normalized_media_type:
        raise RuntimeDriverError("Jupyter output media type is invalid.")
    base64_encoded = normalized_media_type == "application/pdf" or (
        normalized_media_type.startswith("image/")
        and normalized_media_type != "image/svg+xml"
    )
    if base64_encoded:
        if not isinstance(value, str):
            raise RuntimeDriverError(
                "Jupyter binary representation is invalid."
            )
        return RuntimeOutputRepresentation(
            media_type=normalized_media_type,
            encoding="BASE64",
            content=value,
        )
    if isinstance(value, str):
        content = value
    else:
        try:
            content = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeDriverError(
                "Jupyter output representation is not JSON serializable."
            ) from exc
    return RuntimeOutputRepresentation(
        media_type=normalized_media_type,
        encoding="UTF8",
        content=content,
    )


def error_summary(content: dict[str, Any]) -> str:
    name = str(content.get("ename", "ExecutionError"))
    value = str(content.get("evalue", ""))
    return f"{name}: {value}"[:2000]


def message_limit_closed(exc: BaseException) -> bool:
    """Recognize a bounded receive failure without exposing frame content."""

    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, PayloadTooBig):
            return True
        for close_frame_name in ("rcvd", "sent"):
            close_frame = getattr(current, close_frame_name, None)
            if getattr(close_frame, "code", None) == 1009:
                return True
        current = current.__cause__ or current.__context__
    return False
