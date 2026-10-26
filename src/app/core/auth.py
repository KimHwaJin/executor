"""Resolve API callers exclusively from the X-User-ID header."""

from uuid import UUID

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.repositories.user_repository import UserRepository


async def get_current_user_id(
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
    db: AsyncSession = Depends(get_db),
) -> UUID:
    if not x_user_id or not x_user_id.strip():
        raise HTTPException(status_code=401, detail="X-User-ID is required.")
    try:
        user_id = UUID(x_user_id)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid X-User-ID.") from exc

    if await UserRepository.get_active(db, user_id) is None:
        # Do not distinguish deleted users from unknown users.
        raise HTTPException(status_code=401, detail="Invalid X-User-ID.")
    return user_id


async def get_stream_user_id(
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
    db: AsyncSession = Depends(get_db, scope="function"),
) -> UUID:
    """Release the authentication DB connection before an SSE body starts."""
    return await get_current_user_id(x_user_id=x_user_id, db=db)
