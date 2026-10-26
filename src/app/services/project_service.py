from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DeleteYN, ProjectMemberRole
from app.models.common.message_model import MessageModel
from app.models.common.project_model import (
    ProjectMemberModel,
    ProjectModel
)
from app.models.common.session_model import SessionModel
from app.repositories.project_repository import ProjectRepository
from app.repositories.user_repository import UserRepository
from app.schemas.common.project_schema import (
    ProjectCreate,
    ProjectDeleteResult,
    ProjectRead,
    ProjectUpdate,
    SessionSummary,
)
from app.services.cascade_service import (
    soft_delete_messages_for_sessions,
    soft_delete_sessions
)
from app.services.helpers import normalize_name, utc_now


class ProjectService:
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
    async def create(
        db: AsyncSession,
        user_id: UUID,
        payload: ProjectCreate,
    ) -> ProjectModel:
        await ProjectService._require_user(db, user_id)

        project_name = normalize_name(payload.project_name)
        if not project_name:
            # 요구사항: 공란은 default로 보지만 default가 이미 있으므로 생성하지 않음.
            raise HTTPException(
                status_code=409,
                detail="project_name 공란은 default Project로 간주되며 이미 존재합니다.",
            )

        if await ProjectRepository.get_active_by_name(
            db,
            user_id=user_id,
            project_name=project_name,
        ):
            raise HTTPException(status_code=409, detail="동일한 Project 이름이 이미 존재합니다.")

        project = ProjectModel(
            user_id=user_id,
            project_name=project_name,
            system_prompt=payload.system_prompt,
            prompt_version=1,
            is_default=False,
            delete_yn=DeleteYN.N,
        )
        db.add(project)
        await db.flush()

        db.add(
            ProjectMemberModel(
                project_id=project.project_id,
                user_id=user_id,
                member_role=ProjectMemberRole.OWNER,
            )
        )

        await db.commit()
        await db.refresh(project)
        return project

    @staticmethod
    async def read(
        db: AsyncSession,
        user_id: UUID,
        project_id: UUID,
    ) -> ProjectRead:
        project = await ProjectService._require_project(db, user_id, project_id)

        rows = (
            await db.execute(
                select(
                    SessionModel,
                    func.count(MessageModel.message_id).label("message_count"),
                )
                .outerjoin(
                    MessageModel,
                    (MessageModel.session_id == SessionModel.session_id)
                    & (MessageModel.delete_yn == DeleteYN.N),
                )
                .where(
                    SessionModel.project_id == project_id,
                    SessionModel.user_id == user_id,
                    SessionModel.delete_yn == DeleteYN.N,
                )
                .group_by(SessionModel.session_id)
                .order_by(SessionModel.updated_at.desc())
            )
        ).all()

        sessions = [
            SessionSummary(
                session_id=session.session_id,
                session_name=session.session_name,
                project_id=session.project_id,
                user_id=session.user_id,
                message_count=count,
                created_at=session.created_at,
                updated_at=session.updated_at,
            )
            for session, count in rows
        ]

        return ProjectRead(
            project_id=project.project_id,
            user_id=project.user_id,
            project_name=project.project_name,
            system_prompt=project.system_prompt,
            prompt_version=project.prompt_version,
            is_default=project.is_default,
            delete_yn=project.delete_yn,
            created_at=project.created_at,
            updated_at=project.updated_at,
            deleted_at=project.deleted_at,
            sessions=sessions,
        )

    @staticmethod
    async def update(
        db: AsyncSession,
        user_id: UUID,
        project_id: UUID,
        payload: ProjectUpdate,
    ) -> ProjectModel:
        project = await ProjectService._require_project(db, user_id, project_id)
        if project.is_default and payload.project_name is not None:
            raise HTTPException(status_code=409, detail="default Project 이름은 변경할 수 없습니다.")

        if payload.project_name is not None:
            project_name = normalize_name(payload.project_name)
            if not project_name:
                raise HTTPException(status_code=422, detail="project_name cannot be blank.")

            duplicate = await ProjectRepository.get_active_by_name(
                db,
                user_id=user_id,
                project_name=project_name,
            )
            if duplicate is not None and duplicate.project_id != project_id:
                raise HTTPException(status_code=409, detail="A project with this name already exists.")
            project.project_name = project_name

        if payload.system_prompt is not None and project.system_prompt != payload.system_prompt:
            project.system_prompt = payload.system_prompt
            project.prompt_version += 1
        await db.commit()
        await db.refresh(project)
        return project

    @staticmethod
    async def delete(
        db: AsyncSession,
        user_id: UUID,
        project_id: UUID,
    ) -> ProjectDeleteResult:
        project = await ProjectService._require_project(db, user_id, project_id)

        session_ids = list(
            (
                await db.scalars(
                    select(SessionModel.session_id).where(
                        SessionModel.project_id == project_id,
                        SessionModel.user_id == user_id,
                        SessionModel.delete_yn == DeleteYN.N,
                    )
                )
            ).all()
        )

        deleted_message_count = await soft_delete_messages_for_sessions(db, session_ids)
        deleted_session_count = await soft_delete_sessions(db, session_ids)

        if project.is_default:
            # default Project 행은 유지하고 내부 대화만 초기화합니다.
            project_deleted = False
            detail = "default Project는 유지하고 하위 Session/Message만 삭제했습니다."
        else:
            project.delete_yn = DeleteYN.Y
            project.deleted_at = utc_now()
            project_deleted = True
            detail = "Project와 하위 Session/Message를 삭제했습니다."

        await db.commit()
        return ProjectDeleteResult(
            project_id=project_id,
            project_deleted=project_deleted,
            deleted_session_count=deleted_session_count,
            deleted_message_count=deleted_message_count,
            detail=detail,
        )

