from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user_id
from app.core.database import get_db
from app.core.pagination import ListParams, fetch_page, list_params
from app.models.common.jupyter_server_model import JupyterServerModel
from app.schemas.common.api_schema import Page
from app.schemas.common.jupyter_server_schema import (
    JupyterServerResource,
)
from app.services.jupyter_server_service import JupyterServerService


router = APIRouter(prefix="/jupyter-servers", tags=["jupyter-servers"])


def _resource(server: JupyterServerModel) -> JupyterServerResource:
    # token_ciphertext도 응답 schema에 전달하지 않습니다.
    return JupyterServerResource(
        jupyter_server_id=server.jupyter_server_id,
        name=server.name,
        endpoint=server.endpoint,
        has_token=bool(server.token_ciphertext),
        health_status=server.health_status,
        last_http_status=server.last_http_status,
        last_latency_ms=server.last_latency_ms,
        last_error=server.last_error,
        last_checked_at=server.last_checked_at,
        created_at=server.created_at,
        updated_at=server.updated_at,
        deleted_at=server.deleted_at,
    )


@router.get("", response_model=Page[JupyterServerResource])
async def list_jupyter_servers(params: ListParams = Depends(list_params), user_id: UUID = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    items, page = await fetch_page(
        db,
        select(JupyterServerModel).where(
            JupyterServerModel.created_by_user_id == user_id,
            JupyterServerModel.deleted_at.is_(None),
        ),
        model=JupyterServerModel,
        id_name="jupyter_server_id",
        params=params,
    )
    return {"items": [_resource(item) for item in items], "page": page}


@router.get("/{server_id}", response_model=JupyterServerResource)
async def read_jupyter_server(server_id: UUID, user_id: UUID = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    return _resource(await JupyterServerService.get(db, user_id, server_id))


@router.post("/{server_id}/health", response_model=JupyterServerResource)
async def check_jupyter_server_health(server_id: UUID, user_id: UUID = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    return _resource(await JupyterServerService.refresh_health(db, user_id, server_id))

