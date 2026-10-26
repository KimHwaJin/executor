from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DeleteYN
from app.models import MessageModel, SessionModel


class MessageRepository:
    @staticmethod
    async def get_owned_active(
        db: AsyncSession,
        *,
        user_id: UUID,
        message_id: UUID,
    ) -> MessageModel | None:
        return await db.scalar(
            select(MessageModel)
            .join(SessionModel, SessionModel.session_id == MessageModel.session_id)
            .where(
                MessageModel.message_id == message_id,
                MessageModel.delete_yn == DeleteYN.N,
                SessionModel.user_id == user_id,
                SessionModel.delete_yn == DeleteYN.N,
            )
        )

    @staticmethod
    async def get_in_session(
        db: AsyncSession,
        *,
        session_id: UUID,
        message_id: UUID,
    ) -> MessageModel | None:
        return await db.scalar(
            select(MessageModel).where(
                MessageModel.message_id == message_id,
                MessageModel.session_id == session_id,
                MessageModel.delete_yn == DeleteYN.N,
            )
        )

