from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import DeleteYN, MessageStatus, MessageType
from app.models.common.message_model import MessageModel
from app.models.common.session_model import SessionModel
from app.repositories.message_repository import MessageRepository
from app.repositories.session_repository import SessionRepository
from app.schemas.common.message_schema import (
    MessageCreate,
    MessageCreateResult,
    MessageDeleteResult,
    MessageRead,
    MessageUpdate,
)
from app.services.helpers import make_session_name, utc_now


class MessageService:
    @staticmethod
    def _normalize_content(payload_content, content_text: str) -> list[dict]:
        if payload_content:
            return [part.model_dump(mode="json", exclude_none=True) for part in payload_content]
        return [{"type": "text", "text": content_text}]

    @staticmethod
    async def _resolve_session(
        db: AsyncSession,
        *,
        user_id: UUID,
        session_id: UUID | None,
        project_id: UUID | None,
    ) -> tuple[SessionModel, bool]:
        if session_id is None:
            raise HTTPException(
                status_code=422,
                detail="MessageService requires an existing session_id.",
            )
        session = await SessionRepository.get_active_by_user(
            db,
            user_id=user_id,
            session_id=session_id,
            for_update=True,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Session을 찾을 수 없습니다.")
        if project_id is not None and session.project_id != project_id:
            raise HTTPException(status_code=409, detail="입력한 project_id와 Session의 소속 Project가 다릅니다.")
        return session, False

    @staticmethod
    async def create(
        db: AsyncSession,
        user_id: UUID,
        payload: MessageCreate,
        *,
        commit: bool = True,
    ) -> MessageCreateResult:
        session, session_created = await MessageService._resolve_session(
            db,
            user_id=user_id,
            session_id=payload.session_id,
            project_id=payload.project_id,
        )

        if payload.client_request_id is not None:
            duplicate = await db.scalar(
                select(MessageModel).where(
                    MessageModel.session_id == session.session_id,
                    MessageModel.client_request_id == payload.client_request_id,
                    MessageModel.delete_yn == DeleteYN.N,
                )
            )
            if duplicate is not None:
                # Message CRUD는 messages 테이블의 멱등 결과만 반환합니다.
                # Agent/LLM 실행 조회는 각 전용 서비스의 책임입니다.
                return MessageCreateResult(
                    session_created=False,
                    project_id=session.project_id,
                    session_id=session.session_id,
                    message=MessageRead.model_validate(duplicate),
                )

        message = MessageModel(
            session_id=session.session_id,
            message_type=payload.message_type,
            content=MessageService._normalize_content(payload.content, payload.content_text),
            content_text=payload.content_text,
            message_status=MessageStatus.COMPLETED,
            client_request_id=payload.client_request_id,
            metadata_json=payload.metadata,
            delete_yn=DeleteYN.N,
        )
        db.add(message)
        await db.flush()

        session.current_leaf_message_id = message.message_id

        # Message CRUD에서 Session이 자동 생성됐거나 이름이 아직 기본값이면
        # 첫 User 메시지를 읽어 Session 제목을 자동 변경합니다.
        if payload.message_type == MessageType.USER and session.session_name == "새 대화":
            session.session_name = make_session_name(payload.content_text)

        if commit:
            await db.commit()
        await db.refresh(message)

        return MessageCreateResult(
            session_created=session_created,
            project_id=session.project_id,
            session_id=session.session_id,
            message=MessageRead.model_validate(message),
        )

    @staticmethod
    async def read(db: AsyncSession, user_id: UUID, message_id: UUID) -> MessageRead:
        message = await MessageRepository.get_owned_active(
            db,
            user_id=user_id,
            message_id=message_id,
        )
        if message is None:
            raise HTTPException(status_code=404, detail="Message를 찾을 수 없습니다.")
        return MessageRead.model_validate(message)

    @staticmethod
    async def update(
        db: AsyncSession,
        user_id: UUID,
        message_id: UUID,
        payload: MessageUpdate,
    ) -> MessageRead:
        message = await MessageRepository.get_owned_active(
            db,
            user_id=user_id,
            message_id=message_id,
        )
        if message is None:
            raise HTTPException(status_code=404, detail="Message를 찾을 수 없습니다.")

        message.content_text = payload.content_text
        message.content = MessageService._normalize_content(payload.content, payload.content_text)

        await db.commit()
        await db.refresh(message)
        return MessageRead.model_validate(message)

    @staticmethod
    async def delete(
        db: AsyncSession,
        user_id: UUID,
        message_id: UUID,
    ) -> MessageDeleteResult:
        root = await MessageRepository.get_owned_active(
            db,
            user_id=user_id,
            message_id=message_id,
        )
        if root is None:
            raise HTTPException(status_code=404, detail="Message를 찾을 수 없습니다.")

        session = await SessionRepository.get_active_by_user(
            db,
            user_id=user_id,
            session_id=root.session_id,
            for_update=True,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Session을 찾을 수 없습니다.")

        now = utc_now()
        await db.execute(
            update(MessageModel)
            .where(MessageModel.message_id == message_id)
            .values(delete_yn=DeleteYN.Y, deleted_at=now)
        )

        if session.current_leaf_message_id == message_id:
            # parent chain이 없으므로 현재 Session의 직전 활성 Message를 leaf로 선택합니다.
            session.current_leaf_message_id = await db.scalar(
                select(MessageModel.message_id)
                .where(
                    MessageModel.session_id == root.session_id,
                    MessageModel.message_id != message_id,
                    MessageModel.delete_yn == DeleteYN.N,
                )
                .order_by(MessageModel.sequence_no.desc())
                .limit(1)
            )

        await db.commit()

        return MessageDeleteResult(
            message_id=message_id,
            deleted_message_count=1,
            current_leaf_message_id=session.current_leaf_message_id,
            detail="Message를 삭제했습니다.",
        )

