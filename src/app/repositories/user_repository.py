from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DeleteYN
from app.models.common.user_model import UserModel


class UserRepository:
    @staticmethod
    async def get_active(db: AsyncSession, user_id: UUID) -> UserModel | None:
        return await db.scalar(
            select(UserModel).where(
                UserModel.user_id == user_id,
                UserModel.delete_yn == DeleteYN.N,
            )
        )

    @staticmethod
    async def get_active_by_name(db: AsyncSession, user_name: str) -> UserModel | None:
        return await db.scalar(
            select(UserModel).where(
                func.lower(UserModel.user_name) == user_name.lower(),
                UserModel.delete_yn == DeleteYN.N,
            )
        )

