"""Task-scoped SSE, backed by the existing durable event sequence."""
import asyncio
import json
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from app.core.database import get_session_factory
from app.schemas.common.workflow_task_schema import TaskEventEnvelope
from app.services.task_event_service import TaskEventService
from app.services.workflow_task_service import task_snapshot
from config import settings


def parse_cursor(value):
    if value is None:
        return None
    try:
        sequence = int(value)
        if sequence < 0:
            raise ValueError
        return sequence
    except ValueError as exc:
        raise HTTPException(400, "Last-Event-ID must be a non-negative integer.") from exc


def validate_cursor(sequence, snapshot):
    if sequence is not None and sequence > snapshot.last_event_sequence:
        raise HTTPException(409, {"code": "CURSOR_AHEAD", "task_id": str(snapshot.task_id),
                                  "snapshot_url": f"/api/v1/tasks/{snapshot.task_id}"})


def frame(snapshot, event_type, data, *, sequence=None, event=None, cursor=False):
    number = snapshot.last_event_sequence if sequence is None else sequence
    envelope = TaskEventEnvelope(
        type=event_type, sequence=number, session_id=snapshot.session_id,
        task_id=snapshot.task_id, run_id=event.run_id if event else snapshot.current_run_id,
        event_id=event.task_event_id if event else None,
        occurred_at=event.created_at if event else datetime.now(timezone.utc), data=data,
    )
    prefix = f"id: {number}\n" if cursor else ""
    return prefix + f"event: {event_type}\ndata: {envelope.model_dump_json()}\n\n"


def fingerprint(snapshot):
    # Lease heartbeats touch updated_at; they are not frontend state changes.
    value = snapshot.model_dump(mode="json", exclude={"updated_at", "submission_run_id", "last_event_sequence"})
    return json.dumps(value, sort_keys=True, default=str)


def task_stream_response(request, user_id, task_id, initial_sequence=None):
    async def generate():
        sequence = initial_sequence
        previous = None
        last_heartbeat = time.monotonic()
        while not await request.is_disconnected():
            missing = False
            async with get_session_factory()() as db:
                try:
                    snapshot = await task_snapshot(db, user_id, task_id)
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
                    missing = True
                if not missing:
                    if sequence is None:
                        # No replay requested: start from one consistent snapshot.
                        sequence = snapshot.last_event_sequence
                        batch = []
                    else:
                        batch = await TaskEventService.list_after_task(
                            db, task_id=task_id, sequence=sequence,
                            limit=settings.sse_event_batch_size,
                            through_sequence=snapshot.last_event_sequence,
                        )
            if missing:
                yield 'event: error\ndata: {"code":"TASK_UNAVAILABLE"}\n\n'
                return
            if sequence > snapshot.last_event_sequence:
                yield frame(snapshot, "stream.closed", {"reason": "resync_required",
                            "snapshot_url": f"/api/v1/tasks/{task_id}"})
                return
            for event in batch:
                if event.sequence != sequence + 1:
                    yield frame(snapshot, "stream.closed", {"reason": "resync_required",
                                "snapshot_url": f"/api/v1/tasks/{task_id}"})
                    return
                sequence = event.sequence
                yield frame(snapshot, event.event_type, event.payload,
                            sequence=sequence, event=event, cursor=True)
            drained = sequence >= snapshot.last_event_sequence
            if not batch and not drained:
                # Cursor points into a missing/expired event range. Never silently stall.
                yield frame(snapshot, "stream.closed", {"reason": "resync_required",
                            "snapshot_url": f"/api/v1/tasks/{task_id}"})
                return
            current = fingerprint(snapshot)
            if drained and current != previous:
                # Replay old events BEFORE applying the newer snapshot.
                yield frame(snapshot, "task.snapshot", snapshot.model_dump(mode="json"), cursor=True)
                previous = current
            if drained and snapshot.task_finished:
                yield frame(snapshot, "stream.closed", {"reason": "task_finished", "status": snapshot.status.value})
                return
            now = time.monotonic()
            if now - last_heartbeat >= settings.sse_heartbeat_seconds:
                yield ": heartbeat\n\n"
                last_heartbeat = now
            await asyncio.sleep(settings.sse_poll_interval_seconds)
    return StreamingResponse(generate(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })
