"""REST and SSE transports for the same durable workflow submission."""
import asyncio
import json
import time
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_stream_user_id as workflow_auth
from app.core.database import get_db, get_session_factory
from app.schemas.common.workflow_run_schema import WorkflowRunRequest
from app.services.task_event_service import TaskEventService
from app.services.workflow_run_service import owned_run, submit_workflow, workflow_resource, workflow_status
from config import settings
from app.schemas.common.workflow_task_schema import TaskSnapshot
from app.services.workflow_task_service import task_snapshot
from app.services.workflow_task_stream import parse_cursor, task_stream_response

router = APIRouter(prefix="/workflow_run", tags=["workflow run"])


def event_cursor(value):
    try:
        sequence = int(value or "0")
        if sequence < 0:
            raise ValueError
        return sequence
    except ValueError as exc:
        raise HTTPException(400, "Last-Event-ID must be a non-negative integer.") from exc


def sse_frame(event, payload, sequence=None):
    prefix = f"id: {sequence}\n" if sequence is not None else ""
    return prefix + f"event: {event}\ndata: " + json.dumps(
        payload, ensure_ascii=False, default=str, separators=(",", ":"),
    ) + "\n\n"


def stream_response(request, user_id, run_id, initial_sequence):
    async def events():
        sequence = initial_sequence
        previous_status = None
        last_heartbeat = time.monotonic()
        while not await request.is_disconnected():
            async with get_session_factory()() as db:
                try:
                    # Read status first: a terminal status means all final events
                    # have already committed before the following event query.
                    run = await owned_run(db, user_id, run_id)
                except HTTPException:
                    yield sse_frame("error", {"code": "RUN_UNAVAILABLE"})
                    return
                state = workflow_status(run)
                batch = await TaskEventService.list_after(
                    db, run_id=run_id, sequence=sequence,
                    limit=settings.sse_event_batch_size,
                )
            # An initial snapshot is sent even on reconnect to a finished run.
            if previous_status is None:
                yield sse_frame("status", state)
                previous_status = state
            for event in batch:
                sequence = event.sequence
                yield sse_frame(event.event_type, event.payload, sequence)
            drained = len(batch) < settings.sse_event_batch_size
            if drained and state != previous_status:
                yield sse_frame("status", state)
                previous_status = state
            if drained and state["stream_finished"]:
                yield sse_frame("end", state)
                return
            now = time.monotonic()
            if now - last_heartbeat >= settings.sse_heartbeat_seconds:
                yield ": heartbeat\n\n"
                last_heartbeat = now
            await asyncio.sleep(settings.sse_poll_interval_seconds)
    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


async def submission(
    payload: WorkflowRunRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_dtest_mock_llm: str | None = Header(default=None, alias="X-DTest-Mock-LLM"),
    x_dtest_mock_llm_sleep: str | None = Header(default=None, alias="X-DTest-Mock-LLM-Sleep"),
    user_id: UUID = Depends(workflow_auth, scope="function"),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    metadata = {}
    if str(x_dtest_mock_llm or "").strip().lower() in {"1", "true", "yes"}:
        metadata["dtest_mock_llm"] = True
        if x_dtest_mock_llm_sleep is not None:
            metadata["dtest_mock_llm_sleep_seconds"] = x_dtest_mock_llm_sleep
    return await submit_workflow(payload, idempotency_key, user_id, db, metadata_extra=metadata)


@router.post("", status_code=202, response_model=TaskSnapshot)
async def create_workflow_run(
    response: Response, run=Depends(submission, scope="function"),
    user_id: UUID = Depends(workflow_auth, scope="function"),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    response.headers["Location"] = f"/api/v1/tasks/{run.task_id}"
    snapshot = await task_snapshot(db, user_id, run.task_id)
    snapshot.submission_run_id = run.run_id
    return snapshot


@router.post("/stream")
async def create_workflow_run_stream(
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    user_id: UUID = Depends(workflow_auth, scope="function"),
    db: AsyncSession = Depends(get_db, scope="function"),
    payload: WorkflowRunRequest = ...,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_dtest_mock_llm: str | None = Header(default=None, alias="X-DTest-Mock-LLM"),
    x_dtest_mock_llm_sleep: str | None = Header(default=None, alias="X-DTest-Mock-LLM-Sleep"),
):
    # Validate the cursor before creating any persistent work.
    sequence = parse_cursor(last_event_id)
    run = await submission(payload, idempotency_key, x_dtest_mock_llm,
                           x_dtest_mock_llm_sleep, user_id, db)
    result = task_stream_response(request, user_id, run.task_id, sequence)
    result.headers["Location"] = f"/api/v1/tasks/{run.task_id}"
    result.headers["X-Task-ID"] = str(run.task_id)
    result.headers["X-Run-ID"] = str(run.run_id)
    return result


@router.get("/{run_id}")
async def read_workflow_run(
    run_id: UUID,
    user_id: UUID = Depends(workflow_auth, scope="function"),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    return workflow_resource(await owned_run(db, user_id, run_id))


@router.get("/{run_id}/stream")
async def stream_workflow_run(
    request: Request, run_id: UUID,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    user_id: UUID = Depends(workflow_auth, scope="function"),
    db: AsyncSession = Depends(get_db, scope="function"),
):
    sequence = event_cursor(last_event_id)
    await owned_run(db, user_id, run_id)
    return stream_response(request, user_id, run_id, sequence)
