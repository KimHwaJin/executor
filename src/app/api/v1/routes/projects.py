from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user_id
from app.core.database import get_db
from app.core.enums import DeleteYN
from app.core.pagination import ListParams, fetch_page, list_params
from app.models import ProjectModel
from app.schemas.common.api_schema import Page, ProjectResource
from app.schemas.common.project_schema import ProjectCreate, ProjectUpdate
from app.services.project_service import ProjectService


router = APIRouter(prefix="/projects", tags=["projects"])


@router.post("", response_model=ProjectResource, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreate,
    response: Response,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    project = await ProjectService.create(db, user_id, payload)
    response.headers["Location"] = f"/api/v1/projects/{project.project_id}"
    return ProjectResource.model_validate(project)


@router.get("", response_model=Page[ProjectResource])
async def list_projects(
    params: ListParams = Depends(list_params),
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    items, page = await fetch_page(
        db,
        select(ProjectModel).where(
            ProjectModel.user_id == user_id,
            ProjectModel.delete_yn == DeleteYN.N,
        ),
        model=ProjectModel,
        id_name="project_id",
        params=params,
    )
    return {"items": [ProjectResource.model_validate(item) for item in items], "page": page}


@router.get("/{project_id}", response_model=ProjectResource)
async def read_project(
    project_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    return ProjectResource.model_validate(await ProjectService.read(db, user_id, project_id))


@router.patch("/{project_id}", response_model=ProjectResource)
async def update_project(
    project_id: UUID,
    payload: ProjectUpdate,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    project = await ProjectService.update(db, user_id, project_id, payload)
    return ProjectResource.model_validate(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    await ProjectService.delete(db, user_id, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

