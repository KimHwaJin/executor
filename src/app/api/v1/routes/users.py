from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.common.user_schema import UserCreate, UserRead, UserUpdate
from app.services.user_service import UserService


router = APIRouter(prefix="/users", tags=["users"])


@router.post("", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, db: AsyncSession = Depends(get_db)):
    return await UserService.create(db, payload)


# UUID 동적 경로보다 먼저 선언하여 "by-name"이 user_id로 파싱되지 않게 합니다.
@router.get("/by-name/{user_name}", response_model=UserRead)
async def read_user_by_name(user_name: str, db: AsyncSession = Depends(get_db)):
    return await UserService.read_by_name(db, user_name)


@router.get("/{user_id}", response_model=UserRead)
async def read_user(user_id: UUID, db: AsyncSession = Depends(get_db)):
    return await UserService.read(db, user_id)


@router.patch("/{user_id}", response_model=UserRead)
async def update_user(
    user_id: UUID,
    payload: UserUpdate,
    db: AsyncSession = Depends(get_db),
):
    return await UserService.update(db, user_id, payload)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: UUID, db: AsyncSession = Depends(get_db)):
    await UserService.delete(db, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

