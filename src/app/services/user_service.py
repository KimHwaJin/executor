from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DeleteYN, ProjectMemberRole
from app.models.common.message_model import MessageModel
from app.models.common.project_model import (
    ProjectMemberModel,
    ProjectModel
)
from app.models.common.session_model import SessionModel
from app.models.common.user_model import UserModel
from app.repositories.user_repository import UserRepository
from app.schemas.common.user_schema import UserCreate, UserUpdate
from app.services.helpers import normalize_name, utc_now


class UserService:
    @staticmethod
    async def read_by_name(db: AsyncSession, user_name: str) -> UserModel:
        """개발용 로그인에서 username을 내부 User UUID로 해석합니다."""
        normalized = normalize_name(user_name)
        if not normalized:
            raise HTTPException(status_code=422, detail="user_name은 공백일 수 없습니다.")
        user = await UserRepository.get_active_by_name(db, normalized)
        if user is None:
            raise HTTPException(status_code=404, detail="User를 찾을 수 없습니다.")
        return user

    @staticmethod
    async def create(db: AsyncSession, payload: UserCreate) -> UserModel:
        user_name = normalize_name(payload.user_name)
        if not user_name:
            raise HTTPException(status_code=422, detail="user_name은 공백일 수 없습니다.")

        if await UserRepository.get_active_by_name(db, user_name):
            raise HTTPException(status_code=409, detail="이미 사용 중인 user_name입니다.")

        user = UserModel(user_name=user_name, delete_yn=DeleteYN.N)
        db.add(user)
        await db.flush()

        # User 생성 시 default Project를 반드시 함께 생성합니다.
        default_project = ProjectModel(
            user_id=user.user_id,
            project_name="default",
            system_prompt="",
            prompt_version=1,
            is_default=True,
            delete_yn=DeleteYN.N,
        )
        db.add(default_project)
        await db.flush()

        db.add(
            ProjectMemberModel(
                project_id=default_project.project_id,
                user_id=user.user_id,
                member_role=ProjectMemberRole.OWNER,
            )
        )

        await db.commit()
        await db.refresh(user)
        return user

    @staticmethod
    async def read(db: AsyncSession, user_id: UUID) -> UserModel:
        user = await UserRepository.get_active(db, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User를 찾을 수 없습니다.")
        return user

    @staticmethod
    async def update(
        db: AsyncSession,
        user_id: UUID,
        payload: UserUpdate,
    ) -> UserModel:
        user = await UserService.read(db, user_id)
        user_name = normalize_name(payload.user_name)
        if not user_name:
            raise HTTPException(status_code=422, detail="user_name은 공백일 수 없습니다.")

        duplicate = await UserRepository.get_active_by_name(db, user_name)
        if duplicate is not None and duplicate.user_id != user_id:
            raise HTTPException(status_code=409, detail="이미 사용 중인 user_name입니다.")

        user.user_name = user_name
        await db.commit()
        await db.refresh(user)
        return user

    @staticmethod
    async def delete(db: AsyncSession, user_id: UUID) -> None:
        user = await UserService.read(db, user_id)
        now = utc_now()

        project_ids = list(
            (
                await db.scalars(
                    select(ProjectModel.project_id).where(
                        ProjectModel.user_id == user_id,
                        ProjectModel.delete_yn == DeleteYN.N,
                    )
                )
            ).all()
        )
        session_ids = list(
            (
                await db.scalars(
                    select(SessionModel.session_id).where(
                        SessionModel.user_id == user_id,
                        SessionModel.delete_yn == DeleteYN.N,
                    )
                )
            ).all()
        )

        if session_ids:
            await db.execute(
                update(MessageModel)
                .where(
                    MessageModel.session_id.in_(session_ids),
                    MessageModel.delete_yn == DeleteYN.N,
                )
                .values(delete_yn=DeleteYN.Y, deleted_at=now)
            )
            await db.execute(
                update(SessionModel)
                .where(SessionModel.session_id.in_(session_ids))
                .values(
                    delete_yn=DeleteYN.Y,
                    deleted_at=now,
                    current_leaf_message_id=None,
                )
            )

        if project_ids:
            await db.execute(
                update(ProjectModel)
                .where(ProjectModel.project_id.in_(project_ids))
                .values(delete_yn=DeleteYN.Y, deleted_at=now)
            )

        user.delete_yn = DeleteYN.Y
        user.deleted_at = now
        await db.commit()

