from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from app.core.auth import get_current_user_id
from app.schemas.common.redis_schema import RedisPingResource
from app.services.redis_service import RedisService


router = APIRouter(prefix="/redis", tags=["redis"])


@router.get("/ping", response_model=RedisPingResource)
async def ping_redis(_user_id: UUID = Depends(get_current_user_id)) -> RedisPingResource:
    """설정된 REDIS_URL broker에 PING을 보냅니다."""

    return await RedisService.ping()

