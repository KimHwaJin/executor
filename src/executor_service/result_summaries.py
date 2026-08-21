"""Small, transport-safe summaries for Jupyter-compatible outputs."""

from collections import Counter
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OutputSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_count: int = Field(ge=0)
    output_types: dict[str, int]
    stream_names: list[str]
    mime_types: list[str]
    has_image: bool
    image_count: int = Field(ge=0)
    has_error: bool


def summarize_outputs(outputs: list[dict[str, Any]]) -> OutputSummary:
    output_types: Counter[str] = Counter()
    stream_names: set[str] = set()
    mime_types: set[str] = set()
    image_count = 0
    has_error = False

    for output in outputs:
        output_type = output.get("output_type")
        if isinstance(output_type, str):
            output_types[output_type] += 1
            has_error = has_error or output_type == "error"
        stream_name = output.get("name")
        if output_type == "stream" and isinstance(stream_name, str):
            stream_names.add(stream_name)
        data = output.get("data")
        if isinstance(data, dict):
            for media_type in data:
                if not isinstance(media_type, str):
                    continue
                mime_types.add(media_type)
                if media_type.startswith("image/"):
                    image_count += 1

    return OutputSummary(
        output_count=len(outputs),
        output_types=dict(sorted(output_types.items())),
        stream_names=sorted(stream_names),
        mime_types=sorted(mime_types),
        has_image=image_count > 0,
        image_count=image_count,
        has_error=has_error,
    )
