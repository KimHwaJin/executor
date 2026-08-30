"""Durable and atomic file operations for shared result storage."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from executor_service.infrastructure._result_storage.errors import (
    ResultStorageError,
)


def atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    atomic_write(
        path,
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8"),
    )


def atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def json_value(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise ResultStorageError(
            "Result metadata must be JSON serializable."
        ) from exc


def utc_now() -> str:
    return datetime.now(UTC).isoformat()
