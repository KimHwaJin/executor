from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user_id
from app.core.database import get_db
from app.core.enums import DeleteYN
from app.core.pagination import ListParams, fetch_page, list_params
from app.models import MessageModel, SessionModel
from app.repositories.project_repository import ProjectRepository
from app.schemas.common.api_schema import MessageResource, Page
from app.schemas.common.message_schema import MessageCreate, MessageCreateResult, MessageUpdate
from app.services.message_service import MessageService
from app.services.session_service import SessionService


router = APIRouter(tags=["messages"])


def _idempotency_key(value: str | None) -> UUID | None:
    if value is None:
        return None
    if not value.strip():
        raise HTTPException(status_code=422, detail="Idempotency-Key cannot be blank.")
    # The existing database column is UUID-based.  Namespacing preserves support
    # for opaque, standards-compliant header values without exposing that detail.
    return uuid5(NAMESPACE_URL, f"message-create:{value}")


@router.post(
    "/messages",
    response_model=MessageCreateResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_message_without_session(
    payload: MessageCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """Create a message, creating a session first when session_id is omitted."""
    header_key = _idempotency_key(idempotency_key)
    if payload.client_request_id and header_key and payload.client_request_id != header_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key does not match client_request_id.")
    if header_key:
        payload.client_request_id = header_key

    # API coordinator가 Session 생성을 담당하고 MessageService에는 기존 session_id만 전달합니다.
    session_created = payload.session_id is None
    if session_created:
        project_id = payload.project_id
        if project_id is None:
            project = await ProjectRepository.get_default(db, user_id=user_id)
            if project is None:
                raise HTTPException(status_code=409, detail="default Project가 없습니다.")
            project_id = project.project_id
        session = await SessionService.create_internal(
            db, user_id=user_id, project_id=project_id, session_name="새 대화"
        )
        payload.session_id = session.session_id

    result = await MessageService.create(db, user_id, payload)
    result.session_created = session_created
    response.headers["Location"] = f"/api/v1/messages/{result.message.message_id}"
    response.headers["X-Session-Id"] = str(result.session_id)
    response.headers["X-Session-Created"] = str(result.session_created).lower()
    return result


@router.post(
    "/sessions/{session_id}/messages",
    response_model=MessageCreateResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_message(
    session_id: UUID,
    payload: MessageCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    payload.session_id = session_id
    header_key = _idempotency_key(idempotency_key)
    if payload.client_request_id and header_key and payload.client_request_id != header_key:
        raise HTTPException(status_code=422, detail="Idempotency-Key does not match client_request_id.")
    if header_key:
        payload.client_request_id = header_key
    result = await MessageService.create(db, user_id, payload)
    response.headers["Location"] = f"/api/v1/messages/{result.message.message_id}"
    return result


@router.get("/sessions/{session_id}/messages", response_model=Page[MessageResource])
async def list_messages(
    session_id: UUID,
    params: ListParams = Depends(list_params),
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    items, page = await fetch_page(
        db,
        # sessions.current_leaf_message_id도 messages를 참조하므로 자동 join은
        # FK 경로가 모호합니다. Message 소속 Session FK를 명시합니다.
        select(MessageModel).join(
            SessionModel,
            SessionModel.session_id == MessageModel.session_id,
        ).where(
            MessageModel.session_id == session_id,
            MessageModel.delete_yn == DeleteYN.N,
            SessionModel.user_id == user_id,
            SessionModel.delete_yn == DeleteYN.N,
        ),
        model=MessageModel,
        id_name="message_id",
        params=params,
    )
    return {"items": [MessageResource.model_validate(item) for item in items], "page": page}


@router.get("/messages/{message_id}", response_model=MessageResource)
async def read_message(
    message_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    return MessageResource.model_validate(await MessageService.read(db, user_id, message_id))


@router.patch("/messages/{message_id}", response_model=MessageResource)
async def update_message(
    message_id: UUID,
    payload: MessageUpdate,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    return MessageResource.model_validate(
        await MessageService.update(db, user_id, message_id, payload)
    )


@router.delete("/messages/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_message(
    message_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await MessageService.delete(db, user_id, message_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

