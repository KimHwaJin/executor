"""Serialization for runtime outputs and result manifests."""

import base64
import binascii
import json
from collections import Counter
from pathlib import Path
from typing import Any

from executor_service.domain.results import (
    ExecutionSourceReference,
    StepResultIdentity,
)
from executor_service.domain.runtime import RuntimeOutputRecord
from executor_service.infrastructure._result_storage.errors import (
    ResultStorageError,
)
from executor_service.infrastructure._result_storage.io import (
    atomic_write,
    json_value,
    sha256,
    utc_now,
)

OUTPUT_KIND_NAMES = {
    "STREAM": "stream",
    "DISPLAY": "display_data",
    "RESULT": "execute_result",
    "ERROR": "error",
}
MEDIA_EXTENSIONS = {
    "application/json": ".json",
    "application/javascript": ".js",
    "application/pdf": ".pdf",
    "application/xml": ".xml",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
    "text/html": ".html",
    "text/markdown": ".md",
    "text/plain": ".txt",
}


class ResultOutputCodec:
    def persist_record(
        self,
        partial: Path,
        *,
        ordinal: int,
        record: RuntimeOutputRecord,
    ) -> dict[str, Any]:
        kind = record.kind.upper()
        if kind not in OUTPUT_KIND_NAMES:
            raise ResultStorageError(f"Unsupported output kind: {kind!r}.")
        representations: list[dict[str, Any]] = []
        for index, representation in enumerate(record.representations):
            media_type = representation.media_type.lower().strip()
            body = representation_body(
                representation.encoding, representation.content
            )
            suffix = media_extension(media_type)
            filename = f"{ordinal:06d}-{kind.lower()}-{index:02d}{suffix}"
            path = partial / "outputs" / filename
            atomic_write(path, body)
            representations.append(
                {
                    "media_type": media_type,
                    "encoding": representation.encoding,
                    "relative_path": f"outputs/{filename}",
                    "size_bytes": len(body),
                    "checksum_sha256": sha256(body),
                    "complete": True,
                    "truncated_in_preview": False,
                    "metadata": json_value(representation.metadata),
                }
            )
        return {
            "ordinal": ordinal,
            "kind": kind,
            "stream_name": record.stream_name,
            "execution_count": record.execution_count,
            "representations": representations,
            "metadata": json_value(record.metadata),
            "created_at": utc_now(),
        }

    def notebook_output(
        self, result_directory: Path, raw_output: object
    ) -> dict[str, object]:
        if not isinstance(raw_output, dict):
            raise ResultStorageError("Step output descriptor is invalid.")
        kind = str(raw_output.get("kind", ""))
        raw_representations = raw_output.get("representations")
        if not isinstance(raw_representations, list):
            raise ResultStorageError(
                "Step output representations are invalid."
            )
        if not all(isinstance(value, dict) for value in raw_representations):
            raise ResultStorageError(
                "Step output representation descriptor is invalid."
            )
        representations = {
            str(value["media_type"]): self._representation_value(
                result_directory, value
            )
            for value in raw_representations
        }
        metadata = raw_output.get("metadata", {})
        if not isinstance(metadata, dict):
            raise ResultStorageError("Step output metadata is invalid.")
        if kind == "STREAM":
            return {
                "output_type": "stream",
                "name": str(raw_output.get("stream_name") or "stdout"),
                "text": str(representations.get("text/plain", "")),
            }
        if kind in {"DISPLAY", "RESULT"}:
            output: dict[str, object] = {
                "output_type": (
                    "execute_result" if kind == "RESULT" else "display_data"
                ),
                "data": representations,
                "metadata": metadata,
            }
            execution_count = raw_output.get("execution_count")
            if kind == "RESULT":
                output["execution_count"] = execution_count
            return output
        if kind == "ERROR":
            traceback = str(representations.get("text/plain", ""))
            return {
                "output_type": "error",
                "ename": str(metadata.get("ename", "ExecutionError")),
                "evalue": str(metadata.get("evalue", "")),
                "traceback": traceback.splitlines(),
            }
        raise ResultStorageError(f"Unsupported Step output kind: {kind!r}.")

    @staticmethod
    def _representation_value(
        result_directory: Path, value: dict[str, Any]
    ) -> object:
        relative_path = Path(str(value.get("relative_path", "")))
        if relative_path.is_absolute() or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            raise ResultStorageError("Step output path is unsafe.")
        path = (result_directory / relative_path).resolve(strict=True)
        try:
            path.relative_to(result_directory)
        except ValueError as exc:
            raise ResultStorageError(
                "Step output path escapes its result."
            ) from exc
        body = path.read_bytes()
        if len(body) != value.get("size_bytes") or sha256(body) != value.get(
            "checksum_sha256"
        ):
            raise ResultStorageError("Step output content checksum failed.")
        media_type = str(value.get("media_type", ""))
        if media_type.startswith("image/") or media_type == "application/pdf":
            return base64.b64encode(body).decode("ascii")
        try:
            text_value = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResultStorageError("Text Step output is not UTF-8.") from exc
        if media_type == "application/json" or media_type.endswith("+json"):
            try:
                return json.loads(text_value)
            except json.JSONDecodeError as exc:
                raise ResultStorageError(
                    "JSON Step output is invalid."
                ) from exc
        return text_value


def identity_json(identity: StepResultIdentity) -> dict[str, object]:
    if identity.sequence < 0 or identity.fencing_token < 1:
        raise ResultStorageError("Step result identity values are invalid.")
    return {
        "execution_id": str(identity.execution_id),
        "operation_id": str(identity.operation_id),
        "step_id": str(identity.step_id),
        "sequence": identity.sequence,
        "execution_attempt_id": str(identity.execution_attempt_id),
        "fencing_token": identity.fencing_token,
    }


def source_json(source: ExecutionSourceReference) -> dict[str, object]:
    return {
        "relative_path": source.relative_path,
        "checksum_sha256": source.checksum_sha256,
        "size_bytes": source.size_bytes,
    }


def output_summary(outputs: list[dict[str, Any]]) -> dict[str, object]:
    output_types: Counter[str] = Counter()
    streams: set[str] = set()
    media_types: set[str] = set()
    image_count = 0
    has_error = False
    for output in outputs:
        kind = str(output["kind"])
        output_types[OUTPUT_KIND_NAMES[kind]] += 1
        has_error = has_error or kind == "ERROR"
        stream_name = output.get("stream_name")
        if kind == "STREAM" and isinstance(stream_name, str):
            streams.add(stream_name)
        for representation in output["representations"]:
            media_type = str(representation["media_type"])
            media_types.add(media_type)
            if media_type.startswith("image/"):
                image_count += 1
    return {
        "output_count": len(outputs),
        "output_types": dict(sorted(output_types.items())),
        "stream_names": sorted(streams),
        "mime_types": sorted(media_types),
        "has_image": image_count > 0,
        "image_count": image_count,
        "has_error": has_error,
    }


def records_digest(records: tuple[RuntimeOutputRecord, ...]) -> str:
    return sha256(
        json.dumps(
            [record_json(record) for record in records],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def record_json(record: RuntimeOutputRecord) -> dict[str, Any]:
    return {
        "kind": record.kind,
        "stream_name": record.stream_name,
        "execution_count": record.execution_count,
        "representations": [
            {
                "media_type": item.media_type,
                "encoding": item.encoding,
                "content": item.content,
                "metadata": json_value(item.metadata),
            }
            for item in record.representations
        ],
        "metadata": json_value(record.metadata),
    }


def representation_body(encoding: str, content: str) -> bytes:
    try:
        if encoding == "UTF8":
            return content.encode("utf-8")
        if encoding == "BASE64":
            return base64.b64decode(content, validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ResultStorageError(
            "Output content does not match its declared encoding."
        ) from exc
    raise ResultStorageError(f"Unsupported output encoding: {encoding!r}.")


def media_extension(media_type: str) -> str:
    if media_type in MEDIA_EXTENSIONS:
        return MEDIA_EXTENSIONS[media_type]
    if media_type.endswith("+json"):
        return ".json"
    if media_type.endswith("+xml"):
        return ".xml"
    if media_type.startswith("image/"):
        return ".img"
    if media_type.startswith("text/"):
        return ".txt"
    return ".bin"
