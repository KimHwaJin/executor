from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user_id
from app.core.database import get_db
from app.core.enums import DeleteYN
from app.core.pagination import ListParams, fetch_page, list_params
from app.models import SessionModel
from app.repositories.session_repository import SessionRepository
from app.schemas.common.api_schema import Page, SessionResource
from app.schemas.common.session_schema import SessionCreate, SessionUpdate
from app.services.session_service import SessionService


router = APIRouter(tags=["sessions"])


async def _owned_session(db: AsyncSession, user_id: UUID, session_id: UUID) -> SessionModel:
    session = await SessionRepository.get_active_by_user(
        db, user_id=user_id, session_id=session_id
    )
    if session is None:
        # The same response is deliberately used for missing and inaccessible IDs.
        raise HTTPException(status_code=404, detail="Session not found.")
    return session


@router.post(
    "/projects/{project_id}/sessions",
    response_model=SessionResource,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    project_id: UUID,
    payload: SessionCreate,
    response: Response,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    session = await SessionService.create(db, user_id, project_id, payload)
    response.headers["Location"] = f"/api/v1/sessions/{session.session_id}"
    return SessionResource.model_validate(session)


@router.get("/projects/{project_id}/sessions", response_model=Page[SessionResource])
async def list_project_sessions(
    project_id: UUID,
    params: ListParams = Depends(list_params),
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await SessionService._require_project(db, user_id, project_id)
    items, page = await fetch_page(
        db,
        select(SessionModel).where(
            SessionModel.user_id == user_id,
            SessionModel.project_id == project_id,
            SessionModel.delete_yn == DeleteYN.N,
        ),
        model=SessionModel,
        id_name="session_id",
        params=params,
    )
    return {"items": [SessionResource.model_validate(item) for item in items], "page": page}


@router.get("/sessions/{session_id}", response_model=SessionResource)
async def read_session(
    session_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    session = await _owned_session(db, user_id, session_id)
    result = await SessionService.read(db, user_id, session.project_id, session_id)
    return SessionResource.model_validate(result)


@router.patch("/sessions/{session_id}", response_model=SessionResource)
async def update_session(
    session_id: UUID,
    payload: SessionUpdate,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    session = await _owned_session(db, user_id, session_id)
    updated = await SessionService.update(db, user_id, session.project_id, session_id, payload)
    return SessionResource.model_validate(updated)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    session = await _owned_session(db, user_id, session_id)
    await SessionService.delete(db, user_id, session.project_id, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

