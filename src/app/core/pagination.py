"""Cursor pagination shared by all collection endpoints."""

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, Query
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.common.api_schema import PageInfo


@dataclass(frozen=True)
class ListParams:
    limit: int = 50
    cursor: str | None = None
    sort: str = "-created_at"
    created_at_from: datetime | None = None
    created_at_to: datetime | None = None


def list_params(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None),
    sort: str = Query(default="-created_at", pattern=r"^-?created_at$"),
    created_at_from: datetime | None = Query(default=None),
    created_at_to: datetime | None = Query(default=None),
) -> ListParams:
    if created_at_from and created_at_to and created_at_from >= created_at_to:
        raise HTTPException(
            status_code=422,
            detail="created_at_from must be earlier than created_at_to.",
        )
    return ListParams(limit, cursor, sort, created_at_from, created_at_to)


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        return datetime.fromisoformat(value["created_at"]), UUID(value["id"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor.") from exc


def make_cursor(item, *, id_name: str) -> str:
    payload = json.dumps(
        {"created_at": item.created_at.isoformat(), "id": str(getattr(item, id_name))},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


async def fetch_page(
    db: AsyncSession,
    stmt,
    *,
    model,
    id_name: str,
    params: ListParams,
) -> tuple[list, PageInfo]:
    created_at = model.created_at
    resource_id = getattr(model, id_name)
    descending = params.sort.startswith("-")

    if params.created_at_from:
        stmt = stmt.where(created_at >= params.created_at_from)
    if params.created_at_to:
        stmt = stmt.where(created_at < params.created_at_to)
    if params.cursor:
        cursor_time, cursor_id = _decode_cursor(params.cursor)
        if descending:
            stmt = stmt.where(
                or_(
                    created_at < cursor_time,
                    and_(created_at == cursor_time, resource_id < cursor_id),
                )
            )
        else:
            stmt = stmt.where(
                or_(
                    created_at > cursor_time,
                    and_(created_at == cursor_time, resource_id > cursor_id),
                )
            )

    order = (created_at.desc(), resource_id.desc()) if descending else (created_at.asc(), resource_id.asc())
    items = list((await db.scalars(stmt.order_by(*order).limit(params.limit + 1))).all())
    has_next = len(items) > params.limit
    items = items[: params.limit]
    return items, PageInfo(
        has_next=has_next,
        next_cursor=make_cursor(items[-1], id_name=id_name) if has_next and items else None,
    )

