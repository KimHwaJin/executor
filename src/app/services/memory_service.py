import logging
import base64
import json
from fastapi import HTTPException
from typing import List
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, udpate
from app.schemas.common.memory_schema import ( 
    MemoryCreate,
    MemoryRead,
    item_to_read,
    MemoryUpdate,
    ListParams,
)
from app.core import store_ops_pg as store_ops
from langgraph.store.postgres import AsyncPostgresStore
from app.models.common.project_model import ProjectModel
from app.core.enums import DeleteYN
from app.core.session_lock_pg import AdvisoryLockError

class MemoryService:
    @staticmethod
    def _decode_page(cursor: str) -> int:

    @staticmethod
    def _decode_page(offset: str) -> int:

    def _byte_size(s: str) -> int:

    @staticmethod
    async def _check_project_exists(
        user_id: UUID, project_id: UUID, session: AsyncSession
    ):
        project = await session.scalar(
            select(ProjectModel).where(
                ProjectModel.user_id == user_id,
                ProjectModel.project_id == project_id,
                ProjectModel.delete_yn == DeleteYN.N,
            )
        )
        logger.info(f"project : {project}")
        if project is None:
            msg = f"Project ID {project_id} not found."
            logger.info(make_massage(massage=msg, project_id=project_id))
            raise HTTPException(status_code=404, detail=msg)


    @staticmethod
    async def list_pg()

    await MemoryService._check_project_exists()
    ns=store_ops.project_ns(project_id=project_id)
    offset=0
    if params.curosr:
        offset = MemoryService._decode_page(cursor=params.cursor)
    items = await store_ops.list_items()
    cursor=None
    if len(items) > params.limit:
        cursor = MemoryService._encode_page(offset=(offset + params.limit))
    return [item_to_read(i) for i in items[: params.limit]], cursor


    @staticmethod
    async def latest_pg()
    
    @staticmethod
    async def read_pg
    
    @staticmethod
    async def create_pg(
        project_id: str,
        user_id: UUID,
        body: MemoryCreate,
        store: AsyncPostgresStore,
        session: AsyncSession,
    ) -> MemoryRead:
        try:
            await MemoryService._check_project_exists(
                user_id =user_id, project_id = project_id, session=session
            )
            ns = store_ops.project_ns(project_id=project_id)
            new_item = await store_ops.create_item(
                store=store,
                namespace=ns,
                value={"text":body.text},
                user_id = str(user_id)
            )
        return item_to_read(new_item)

    @staticmethod
    async def create_and_prune_memories

    async def update_pg

    async def delete_pg