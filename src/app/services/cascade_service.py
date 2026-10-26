from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DeleteYN
from app.models.common.message_model import MessageModel
from app.models.common.session_model import SessionModel
from app.services.helpers import utc_now


async def soft_delete_messages_for_sessions(
    db: AsyncSession,
    session_ids: Iterable[UUID],
) -> int:
    ids = list(session_ids)
    if not ids:
        return 0

    active_ids = list(
        (
            await db.scalars(
                select(MessageModel.message_id).where(
                    MessageModel.session_id.in_(ids),
                    MessageModel.delete_yn == DeleteYN.N,
                )
            )
        ).all()
    )
    if active_ids:
        await db.execute(
            update(MessageModel)
            .where(MessageModel.message_id.in_(active_ids))
            .values(delete_yn=DeleteYN.Y, deleted_at=utc_now())
        )
    return len(active_ids)


async def soft_delete_sessions(
    db: AsyncSession,
    session_ids: Iterable[UUID],
) -> int:
    ids = list(session_ids)
    if not ids:
        return 0

    await db.execute(
        update(SessionModel)
        .where(SessionModel.session_id.in_(ids))
        .values(
            delete_yn=DeleteYN.Y,
            deleted_at=utc_now(),
            current_leaf_message_id=None,
        )
    )
    return len(ids)

