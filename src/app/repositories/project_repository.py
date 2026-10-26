from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DeleteYN
from app.models.common.project_model import ProjectModel


class ProjectRepository:
    @staticmethod
    async def get_owned_active(
        db: AsyncSession,
        *,
        user_id: UUID,
        project_id: UUID,
    ) -> ProjectModel | None:
        return await db.scalar(
            select(ProjectModel).where(
                ProjectModel.project_id == project_id,
                ProjectModel.user_id == user_id,
                ProjectModel.delete_yn == DeleteYN.N,
            )
        )

    @staticmethod
    async def get_default(
        db: AsyncSession,
        *,
        user_id: UUID,
    ) -> ProjectModel | None:
        return await db.scalar(
            select(ProjectModel).where(
                ProjectModel.user_id == user_id,
                ProjectModel.is_default.is_(True),
                ProjectModel.delete_yn == DeleteYN.N,
            )
        )

    @staticmethod
    async def get_active_by_name(
        db: AsyncSession,
        *,
        user_id: UUID,
        project_name: str,
    ) -> ProjectModel | None:
        return await db.scalar(
            select(ProjectModel).where(
                ProjectModel.user_id == user_id,
                func.lower(ProjectModel.project_name) == project_name.lower(),
                ProjectModel.delete_yn == DeleteYN.N,
            )
        )

