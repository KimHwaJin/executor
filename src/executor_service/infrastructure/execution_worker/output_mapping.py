"""Map generic Runtime output dictionaries to durable output records."""

import base64
import json
from typing import Any

from executor_service.domain.runtime import (
    RuntimeDriverError,
    RuntimeOutputRecord,
    RuntimeOutputRepresentation,
)


def output_record(output: dict[str, Any]) -> RuntimeOutputRecord:
    output_type = str(output.get("output_type", ""))
    if output_type == "stream":
        return RuntimeOutputRecord(
            kind="STREAM",
            stream_name=str(output.get("name", "stdout")),
            representations=(
                RuntimeOutputRepresentation(
                    media_type="text/plain",
                    encoding="UTF8",
                    content=str(output.get("text", "")),
                ),
            ),
        )
    if output_type in {"display_data", "execute_result"}:
        data = output.get("data", {})
        if not isinstance(data, dict):
            raise RuntimeDriverError("Runtime display output is invalid.")
        representations = tuple(
            _output_representation(str(media_type), value)
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
        metadata = output.get("metadata", {})
        if not isinstance(metadata, dict):
            raise RuntimeDriverError("Runtime output metadata is invalid.")
        execution_count = output.get("execution_count")
        return RuntimeOutputRecord(
            kind=("RESULT" if output_type == "execute_result" else "DISPLAY"),
            execution_count=(
                int(execution_count) if execution_count is not None else None
            ),
            representations=representations,
            metadata=metadata,
        )
    if output_type == "error":
        traceback = output.get("traceback", [])
        if not isinstance(traceback, list):
            traceback = [str(traceback)]
        return RuntimeOutputRecord(
            kind="ERROR",
            representations=(
                RuntimeOutputRepresentation(
                    media_type="text/plain",
                    encoding="UTF8",
                    content="\n".join(str(line) for line in traceback),
                ),
            ),
            metadata={
                "ename": str(output.get("ename", "Error")),
                "evalue": str(output.get("evalue", "")),
            },
        )
    raise RuntimeDriverError(
        f"Unsupported Runtime output type: {output_type!r}."
    )


def _output_representation(
    media_type: str, value: Any
) -> RuntimeOutputRepresentation:
    normalized = media_type.lower().strip()
    if normalized.startswith("image/"):
        if not isinstance(value, str):
            raise RuntimeDriverError(
                "Runtime image output must be base64 text."
            )
        try:
            base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise RuntimeDriverError(
                "Runtime image output is invalid."
            ) from exc
        return RuntimeOutputRepresentation(
            media_type=normalized,
            encoding="BASE64",
            content=value,
        )
    content = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    )
    return RuntimeOutputRepresentation(
        media_type=normalized,
        encoding="UTF8",
        content=content,
    )
