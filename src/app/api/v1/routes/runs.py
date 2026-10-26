import asyncio
import json
import time
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user_id
from app.core.database import get_db
from app.core.pagination import ListParams, fetch_page, list_params
from app.models import AgentRunLogModel, AgentRunModel
from app.schemas.common.api_schema import Page
from app.schemas.common.run_schema import (
    AgentRunLogResource,
    RunCancel,
    RunCreate,
    RunResource,
)
from app.services.run_service import RunService
from app.services.task_event_service import TaskEventService
from config import settings
from app.core.database import get_session_factory
from app.core.enums import AgentRunStatus

router = APIRouter(tags=["runs"])


@router.post("/sessions/{session_id}/runs", response_model=RunResource, status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    session_id: UUID, payload: RunCreate, response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user_id: UUID = Depends(get_current_user_id), db: AsyncSession = Depends(get_db),
):
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key is required.")
    if len(idempotency_key) > 255:
        raise HTTPException(status_code=422, detail="Idempotency-Key must not exceed 255 characters.")
    run = await RunService.create(db, user_id, session_id, payload, idempotency_key)
    response.headers["Location"] = f"/api/v1/sessions/{session_id}/runs/{run.run_id}"
    return RunResource.model_validate(run)


@router.get("/sessions/{session_id}/runs", response_model=Page[RunResource])
async def list_runs(session_id: UUID, params: ListParams = Depends(list_params), user_id: UUID = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    await RunService._session(db, user_id, session_id)
    items, page = await fetch_page(db, select(AgentRunModel).where(AgentRunModel.session_id == session_id), model=AgentRunModel, id_name="run_id", params=params)
    return {"items": [RunResource.model_validate(item) for item in items], "page": page}


@router.get("/sessions/{session_id}/runs/{run_id}", response_model=RunResource)
async def read_run(session_id: UUID, run_id: UUID, user_id: UUID = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    return RunResource.model_validate(await RunService.read(db, user_id, session_id, run_id))


@router.get(
    "/sessions/{session_id}/runs/{run_id}/logs",
    response_model=list[AgentRunLogResource],
)
async def list_run_logs(
    session_id: UUID,
    run_id: UUID,
    user_id: UUID = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db),
):
    """소유권을 확인한 뒤 Agent별 append-only 실행 로그를 반환합니다."""

    await RunService.read(db, user_id, session_id, run_id)
    logs = (
        await db.scalars(
            select(AgentRunLogModel)
            .where(AgentRunLogModel.run_id == run_id)
            .order_by(AgentRunLogModel.created_at, AgentRunLogModel.log_id)
        )
    ).all()
    return [AgentRunLogResource.model_validate(log) for log in logs]


@router.get("/sessions/{session_id}/runs/{run_id}/join", response_model=RunResource)
async def join_run(session_id: UUID, run_id: UUID, user_id: UUID = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    return RunResource.model_validate(await RunService.read(db, user_id, session_id, run_id))


@router.post("/sessions/{session_id}/runs/{run_id}/cancel", response_model=RunResource, status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(session_id: UUID, run_id: UUID, payload: RunCancel, user_id: UUID = Depends(get_current_user_id), db: AsyncSession = Depends(get_db)):
    return RunResource.model_validate(await RunService.cancel(db, user_id, session_id, run_id, payload))


@router.get("/sessions/{session_id}/runs/{run_id}/stream")
async def stream_run(
    request: Request,
    session_id: UUID,
    run_id: UUID,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    # StreamingResponse가 열린 동안 인증/소유권 확인용 DB session을 붙잡지 않는다.
    user_id: UUID = Depends(get_current_user_id, scope="function"),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """E05-T05: Last-Event-ID 다음 DB event를 재생하고 새 event를 polling합니다."""

    await RunService.read(db, user_id, session_id, run_id)
    try:
        initial_sequence = int(last_event_id or "0")
        if initial_sequence < 0:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be a non-negative integer.") from exc

    async def event_generator():
        sequence = initial_sequence
        last_heartbeat = time.monotonic()
        terminal = {
            AgentRunStatus.SUCCESS,
            AgentRunStatus.ERROR,
            AgentRunStatus.TIMEOUT,
            AgentRunStatus.CANCELED,
            # HITL은 event를 재생한 뒤 연결을 닫고 다음 resume Run에서 새 stream을 사용합니다.
            AgentRunStatus.INTERRUPTED,
        }
        while True:
            if await request.is_disconnected():
                return
            async with get_session_factory()() as event_db:
                events = await TaskEventService.list_after(
                    event_db,
                    run_id=run_id,
                    sequence=sequence,
                    limit=settings.sse_event_batch_size,
                )
                run_status = await event_db.scalar(
                    select(AgentRunModel.status).where(AgentRunModel.run_id == run_id)
                )
            for event in events:
                sequence = event.sequence
                data = json.dumps(event.payload, ensure_ascii=False, default=str, separators=(",", ":"))
                yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {data}\n\n"

            if run_status in terminal and not events:
                return
            now = time.monotonic()
            if now - last_heartbeat >= settings.sse_heartbeat_seconds:
                yield ": heartbeat\n\n"
                last_heartbeat = now
            await asyncio.sleep(settings.sse_poll_interval_seconds)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
