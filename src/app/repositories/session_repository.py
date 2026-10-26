from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DeleteYN
from app.models.common.session_model import SessionModel


class SessionRepository:
    @staticmethod
    async def get_active(
        db: AsyncSession,
        *,
        user_id: UUID,
        project_id: UUID,
        session_id: UUID,
        for_update: bool = False,
    ) -> SessionModel | None:
        stmt = select(SessionModel).where(
            SessionModel.session_id == session_id,
            SessionModel.user_id == user_id,
            SessionModel.project_id == project_id,
            SessionModel.delete_yn == DeleteYN.N,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await db.scalar(stmt)

    @staticmethod
    async def get_active_by_user(
        db: AsyncSession,
        *,
        user_id: UUID,
        session_id: UUID,
        for_update: bool = False,
    ) -> SessionModel | None:
        stmt = select(SessionModel).where(
            SessionModel.session_id == session_id,
            SessionModel.user_id == user_id,
            SessionModel.delete_yn == DeleteYN.N,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await db.scalar(stmt)

