"""Single public transport for the demo; executes existing services in-process."""
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.routes import runs, tasks
from app.core.auth import get_current_user_id
from app.core.database import get_db
from app.core.enums import AgentRunStatus
from app.models import AgentRunModel
from app.repositories.session_repository import SessionRepository
from app.schemas.common.chat_completion_schema import ChatCompletionRequest, Message
from app.schemas.common.run_schema import RunCreate, RunInput, RunResource
from app.schemas.common.task_schema import TaskCancel
from app.services.message_service import MessageService
from app.services.run_service import RunService

router = APIRouter(tags=["chat completion"])


async def submit(payload, key, user_id, db, *, metadata_extra=None):
    if not key or not key.strip() or len(key) > 255:
        raise HTTPException(422, "Idempotency-Key must contain 1–255 nonblank characters.")
    fingerprint = hashlib.sha256(json.dumps(
        payload.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    run_key = "completion:" + hashlib.sha256(key.encode()).hexdigest()
    # Serialize submissions before checking idempotency. Message + Run are committed
    # together by RunService; rejected requests must not leave orphan user messages.
    session = await SessionRepository.get_active_by_user(
        db, user_id=user_id, session_id=payload.session_id, for_update=True,
    )
    if session is None:
        raise HTTPException(404, "Session not found.")
    previous = await db.scalar(select(AgentRunModel).where(
        AgentRunModel.session_id == payload.session_id,
        AgentRunModel.idempotency_key == run_key,
    ))
    if previous is not None:
        if (previous.metadata_json or {}).get("chat_fingerprint") != fingerprint:
            raise HTTPException(409, "Idempotency-Key was used with a different request body.")
        return RunResource.model_validate(previous)
    metadata = {"chat_fingerprint": fingerprint, **(metadata_extra or {})}
    if isinstance(payload, Message):
        saved = await MessageService.create(db, user_id, payload.message_payload(
            client_request_id=uuid5(NAMESPACE_URL, f"completion:{payload.session_id}:{key}"),
        ), commit=False)
        metadata["trigger_message_id"] = str(saved.message.message_id)
        run_payload = RunCreate(
            input=RunInput(messages=[{"role": "user", "content": saved.message.content_text}]),
            metadata=metadata,
        )
    else:
        latest = await db.scalar(select(AgentRunModel).where(
            AgentRunModel.session_id == payload.session_id,
        ).order_by(AgentRunModel.created_at.desc(), AgentRunModel.run_id.desc()).limit(1))
        if latest is None or latest.run_id != payload.run_id or latest.status != AgentRunStatus.INTERRUPTED:
            raise HTTPException(409, "Interrupt is stale or this session is not waiting for input.")
        metadata["resume_run_id"] = str(payload.run_id)
        run_payload = RunCreate(command=payload.command, metadata=metadata)
    run = await RunService.create(db, user_id, payload.session_id, run_payload, run_key)
    return RunResource.model_validate(run)


@router.post("/chat/completion", response_model=None, deprecated=True)
async def chat_completion(
    payload: ChatCompletionRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
    x_dtest_mock_llm: str | None = Header(default=None, alias="X-DTest-Mock-LLM"),
    x_dtest_mock_llm_sleep: str | None = Header(default=None, alias="X-DTest-Mock-LLM-Sleep"),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    """Dispatch by type. message/resume return 202; stream returns SSE.

    All authenticated operations retain the existing ownership checks. The DB
    dependency closes before SSE starts, so an idle subscriber holds no DB lease.
    """
    kind = payload.type
    user_id = await get_current_user_id(
        x_user_id=x_user_id,
        db=db,
    )
    auth = {"user_id": user_id, "db": db}
    if kind == "status":
        return await runs.read_run(payload.session_id, payload.run_id, **auth)
    if kind == "stream":
        return await tasks.stream_task(request, payload.task_id, last_event_id=str(payload.after), **auth)
    if kind == "cancel":
        response.status_code = 202
        return await tasks.cancel_task(payload.task_id, TaskCancel(reason=payload.reason), **auth)
    response.status_code = 202
    metadata_extra = {}
    if str(x_dtest_mock_llm or "").strip().lower() in {"1", "true", "yes"}:
        metadata_extra["dtest_mock_llm"] = True
        if x_dtest_mock_llm_sleep is not None:
            metadata_extra["dtest_mock_llm_sleep_seconds"] = x_dtest_mock_llm_sleep
    return await submit(
        payload,
        idempotency_key,
        user_id,
        db,
        metadata_extra=metadata_extra,
    )
