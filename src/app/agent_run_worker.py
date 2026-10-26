"""Gaia Router lifespan에서 실행되는 PostgreSQL 기반 Agent Run Worker."""

from __future__ import annotations

import asyncio
import socket
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from app.core.database import get_session_factory
from app.core.enums import AgentRunStatus, TaskStatus
from app.models.common.agent_run_model import AgentRunModel
from app.models.common.task_model import TaskModel
from app.schemas.common.run_schema import RunCreate
from app.services.run_service import RunService
from app.services.task_service import TaskService
from app.services.helpers import utc_now


async def claim_one(worker_id: str = "") -> tuple[UUID, UUID, UUID, RunCreate, str] | None:
    """pending Run 하나를 원자 점유하고 실행에 필요한 immutable 값만 반환합니다."""

    async with get_session_factory()() as db:
        run = await db.scalar(
            select(AgentRunModel)
            .where(
                AgentRunModel.status == AgentRunStatus.PENDING,
                or_(
                    AgentRunModel.next_attempt_at.is_(None),
                    AgentRunModel.next_attempt_at <= utc_now(),
                ),
            )
            .order_by(AgentRunModel.created_at, AgentRunModel.run_id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if run is None:
            return None
        task = await db.scalar(
            select(TaskModel)
            .where(TaskModel.task_id == run.task_id)
            .with_for_update()
        )
        if task is None or task.status != TaskStatus.PENDING:
            # 손상된 queue row를 무한 재조회하지 않도록 명시적으로 실패시킵니다.
            run.status = AgentRunStatus.ERROR
            run.failure = {"message": "Queued Run has no pending Task."}
            await db.commit()
            return None

        metadata = dict(run.metadata_json or {})
        user_id = UUID(str(metadata["requested_by_user_id"]))
        owner = f"gaia:{socket.gethostname()}"
        if worker_id:
            owner = f"{owner}:{worker_id}"
        TaskService.start_execution(task, owner=owner)
        run.status = AgentRunStatus.RUNNING
        # row lock 안에서 증가하므로 여러 Pod가 같은 attempt 번호를 가질 수 없습니다.
        run.attempt_count += 1
        run.next_attempt_at = None
        run.started_at = utc_now()
        await db.commit()
        payload = RunCreate(
            input=run.input_json,
            command=run.command_json,
            metadata=metadata,
            multitask_strategy=run.multitask_strategy,
            stream_mode=run.stream_mode,
            stream_resumable=run.stream_resumable,
            on_disconnect=run.on_disconnect,
        )
        return run.run_id, user_id, run.session_id, payload, run.idempotency_key


async def _close_session(session: AsyncSession) -> None:
    """세션 종료가 막힌 트랜잭션을 기다리면 워커 슬롯을 grace 뒤에 놓습니다."""
    close_task = asyncio.create_task(session.close())
    finished, _ = await asyncio.wait(
        {close_task},
        timeout=settings.agent_run_turn_cancel_grace_seconds,
    )
    if close_task not in finished:
        close_task.cancel()


async def execute_claimed(item: tuple[UUID, UUID, UUID, RunCreate, str]) -> None:
    """요청 DB session과 분리된 session에서 이미 점유한 Graph Run을 실행합니다."""

    _run_id, user_id, session_id, payload, key = item
    db = get_session_factory()()
    try:
        try:
            await RunService.create(
                db, user_id, session_id, payload, key, _execute_existing=True
            )
        except Exception:
            # RunService가 오류 상태/Task 잠금 해제를 DB에 먼저 기록합니다.
            # Worker loop 자체는 한 Run 실패 때문에 종료하지 않습니다.
            return
    finally:
        await _close_session(db)


async def run_forever() -> None:
    """여러 Gaia Pod가 동시에 실행해도 SKIP LOCKED로 한 Pod만 Run을 점유합니다."""

    interval = max(0.05, settings.agent_worker_poll_interval_seconds)
    while True:
        item = await claim_one()
        if item is None:
            await asyncio.sleep(interval)
            continue
        await execute_claimed(item)
