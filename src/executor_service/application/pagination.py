"""Opaque cursor primitives shared by REST and MCP query adapters."""

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, overload
from uuid import UUID

from executor_service.domain.errors import InvalidCursorError


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None

    def __iter__(self) -> Iterator[T]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    @overload
    def __getitem__(self, index: int) -> T: ...

    @overload
    def __getitem__(self, index: slice) -> list[T]: ...

    def __getitem__(self, index: int | slice) -> T | list[T]:
        return self.items[index]


def encode_time_cursor(kind: str, created_at: datetime, item_id: UUID) -> str:
    value = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
    return _encode(
        {
            "v": 1,
            "kind": kind,
            "created_at": value.astimezone(UTC).isoformat(),
            "id": str(item_id),
        }
    )


def decode_time_cursor(cursor: str, kind: str) -> tuple[datetime, UUID]:
    payload = _decode(cursor, kind)
    try:
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        item_id = UUID(str(payload["id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("Cursor payload is invalid.") from exc
    if created_at.tzinfo is None:
        raise InvalidCursorError("Cursor timestamp must include a timezone.")
    return created_at, item_id


def encode_integer_cursor(kind: str, value: int) -> str:
    return _encode({"v": 1, "kind": kind, "value": value})


def decode_integer_cursor(cursor: str, kind: str) -> int:
    payload = _decode(cursor, kind)
    value = payload.get("value")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidCursorError("Cursor position is invalid.")
    return value


def _encode(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(cursor: str, expected_kind: str) -> dict[str, Any]:
    if not cursor or len(cursor) > 2048:
        raise InvalidCursorError("Cursor is empty or too long.")
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(cursor + padding, altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("Cursor is malformed.") from exc
    if not isinstance(payload, dict):
        raise InvalidCursorError("Cursor payload must be an object.")
    if payload.get("v") != 1 or payload.get("kind") != expected_kind:
        raise InvalidCursorError("Cursor does not belong to this collection.")
    return payload
