from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DeleteYN
from app.models.common.message_model import MessageModel
from app.models.common.session_model import SessionModel
from app.repositories.project_repository import ProjectRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.user_repository import UserRepository
from app.schemas.common.message_schema import MessageRead
from app.schemas.common.session_schema import (
    SessionCreate,
    SessionDeleteResult,
    SessionRead,
    SessionUpdate,
)
from app.services.cascade_service import (
    soft_delete_messages_for_sessions,
    soft_delete_sessions
)
from app.services.helpers import normalize_name


class SessionService:
    @staticmethod
    async def _require_user(db: AsyncSession, user_id: UUID):
        user = await UserRepository.get_active(db, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User를 찾을 수 없습니다.")
        return user

    @staticmethod
    async def _require_project(db: AsyncSession, user_id: UUID, project_id: UUID):
        project = await ProjectRepository.get_owned_active(
            db,
            user_id=user_id,
            project_id=project_id,
        )
        if project is None:
            raise HTTPException(status_code=404, detail="Project를 찾을 수 없습니다.")
        return project

    @staticmethod
    async def create_internal(
        db: AsyncSession,
        *,
        user_id: UUID,
        project_id: UUID,
        session_name: str | None = None,
        settings: dict | None = None,
    ) -> SessionModel:
        await SessionService._require_user(db, user_id)
        await SessionService._require_project(db, user_id, project_id)

        name = normalize_name(session_name or "") or "새 대화"
        session = SessionModel(
            user_id=user_id,
            project_id=project_id,
            session_name=name,
            settings=settings or {},
            delete_yn=DeleteYN.N,
        )
        db.add(session)
        await db.flush()
        return session

    @staticmethod
    async def create(
        db: AsyncSession,
        user_id: UUID,
        project_id: UUID,
        payload: SessionCreate,
    ) -> SessionModel:
        session = await SessionService.create_internal(
            db,
            user_id=user_id,
            project_id=project_id,
            session_name=payload.session_name,
            settings=payload.settings,
        )
        await db.commit()
        await db.refresh(session)
        return session

    @staticmethod
    async def read(
        db: AsyncSession,
        user_id: UUID,
        project_id: UUID,
        session_id: UUID,
    ) -> SessionRead:
        session = await SessionRepository.get_active(
            db,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Session을 찾을 수 없습니다.")

        messages = list(
            (
                await db.scalars(
                    select(MessageModel)
                    .where(
                        MessageModel.session_id == session_id,
                        MessageModel.delete_yn == DeleteYN.N,
                    )
                    .order_by(MessageModel.sequence_no.asc())
                )
            ).all()
        )

        return SessionRead(
            session_id=session.session_id,
            session_name=session.session_name,
            project_id=session.project_id,
            user_id=session.user_id,
            current_leaf_message_id=session.current_leaf_message_id,
            settings=session.settings,
            delete_yn=session.delete_yn,
            created_at=session.created_at,
            updated_at=session.updated_at,
            deleted_at=session.deleted_at,
            messages=[MessageRead.model_validate(item) for item in messages],
        )

    @staticmethod
    async def update(
        db: AsyncSession,
        user_id: UUID,
        current_project_id: UUID,
        session_id: UUID,
        payload: SessionUpdate,
    ) -> SessionModel:
        session = await SessionRepository.get_active(
            db,
            user_id=user_id,
            project_id=current_project_id,
            session_id=session_id,
            for_update=True,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Session을 찾을 수 없습니다.")

        if payload.session_name is not None:
            name = normalize_name(payload.session_name)
            if not name:
                raise HTTPException(status_code=422, detail="session_name은 공백일 수 없습니다.")
            session.session_name = name

        if payload.target_project_id is not None:
            await SessionService._require_project(db, user_id, payload.target_project_id)
            session.project_id = payload.target_project_id

        await db.commit()
        await db.refresh(session)
        return session

    @staticmethod
    async def delete(
        db: AsyncSession,
        user_id: UUID,
        project_id: UUID,
        session_id: UUID,
    ) -> SessionDeleteResult:
        session = await SessionRepository.get_active(
            db,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            for_update=True,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Session을 찾을 수 없습니다.")

        deleted_message_count = await soft_delete_messages_for_sessions(db, [session_id])
        await soft_delete_sessions(db, [session_id])
        await db.commit()

        return SessionDeleteResult(
            session_id=session_id,
            deleted_message_count=deleted_message_count,
            detail="Session과 하위 Message를 삭제했습니다.",
        )

