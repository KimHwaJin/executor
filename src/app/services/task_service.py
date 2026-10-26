import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from app.core.database import get_session_factory
from app.core.enums import TaskStatus
from app.core.enums import AgentRunStatus
from app.models.common.agent_run_model import AgentRunModel
from app.models.common.task_model import TaskModel
from app.services.helpers import utc_now


class TaskService:
    @staticmethod
    async def attach_graph_task_for_run(
        db: AsyncSession,
        *,
        run_id: UUID,
        graph_task_id: UUID,
    ) -> None:
        """Link the graph/Executor task identity to its durable CRUD Task."""

        run = await db.get(AgentRunModel, run_id)
        if run is None or run.task_id is None:
            return
        task = await db.get(TaskModel, run.task_id)
        if task is None:
            return
        if task.graph_task_id is not None and task.graph_task_id != graph_task_id:
            raise RuntimeError(
                "CRUD Task is already linked to a different graph_task_id: "
                f"task_id={task.task_id}, current={task.graph_task_id}, "
                f"incoming={graph_task_id}"
            )
        task.graph_task_id = graph_task_id
        await db.commit()

    """tasks 테이블 CRUD와 E03 Session lock lifecycle만 담당합니다."""

    EXECUTOR_OWNER = "executor:waiting_report"

    @staticmethod
    def wait_for_executor(task: TaskModel) -> None:
        # Durable external wait: keep the active unique index occupied without
        # an API-worker lease that the reconciler could incorrectly expire.
        task.status = TaskStatus.RUNNING
        task.lock_owner = TaskService.EXECUTOR_OWNER
        task.lock_token = task.heartbeat_at = task.lease_expires_at = None
        task.completed_at = None

    ACTIVE_STATUSES = {TaskStatus.PENDING, TaskStatus.RUNNING}
    TERMINAL_STATUSES = {TaskStatus.SUCCESS, TaskStatus.ERROR, TaskStatus.TIMEOUT, TaskStatus.CANCELED}

    @staticmethod
    def retry_delay_seconds(attempt_count: int) -> float:
        """Graph 예외와 Worker 유실 복구가 동일한 지수 backoff 정책을 사용합니다."""
        base = max(0.0, settings.agent_worker_retry_backoff_seconds)
        ceiling = max(base, settings.agent_worker_retry_max_backoff_seconds)
        return min(ceiling, base * (2 ** max(0, attempt_count - 1)))

    @staticmethod
    def create_model(*, session_id: UUID, idempotency_key: str, owner: str) -> TaskModel:
        now = utc_now()
        return TaskModel(
            session_id=session_id,
            idempotency_key=idempotency_key,
            lock_owner=owner,
            lock_token=uuid4(),
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=settings.task_lease_seconds),
        )

    @staticmethod
    def transition(task: TaskModel, status: TaskStatus, *, failure_reason: str | None = None) -> None:
        """Task 생애주기와 실행 lease를 함께 전환합니다.

        waiting_input은 Job이 살아 있는 상태지만 실행 중이 아니므로 Session lease만 풉니다.
        """
        task.status = status
        task.failure_reason = failure_reason
        if status in TaskService.TERMINAL_STATUSES:
            task.completed_at = utc_now()
        if status not in TaskService.ACTIVE_STATUSES:
            task.lock_owner = task.lock_token = task.heartbeat_at = task.lease_expires_at = None

    @staticmethod
    def start_execution(task: TaskModel, *, owner: str) -> UUID:
        """waiting_input Task를 새 Run이 실행할 수 있도록 원자 잠금 상태로 되돌립니다."""
        now = utc_now()
        token = uuid4()
        task.status = TaskStatus.RUNNING
        task.lock_owner = owner
        task.lock_token = token
        task.heartbeat_at = now
        task.lease_expires_at = now + timedelta(seconds=settings.task_lease_seconds)
        task.failure_reason = None
        task.completed_at = None
        return token

    @staticmethod
    async def attach_trigger(db: AsyncSession, *, task_id: UUID, message_id: UUID) -> None:
        """E03-T02: Graph가 저장한 원본 사용자 Message를 분석 Task에 연결합니다."""
        await db.execute(update(TaskModel).where(TaskModel.task_id == task_id).values(trigger_message_id=message_id))
        await db.commit()

    @staticmethod
    async def heartbeat(db: AsyncSession, *, task_id: UUID, lock_token: UUID) -> bool:
        now = utc_now()
        result = await db.execute(
            update(TaskModel).where(
                TaskModel.task_id == task_id,
                TaskModel.lock_token == lock_token,
                TaskModel.status.in_(TaskService.ACTIVE_STATUSES),
            ).values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=settings.task_lease_seconds))
        )
        await db.commit()
        return bool(result.rowcount)

    @staticmethod
    @asynccontextmanager
    async def lease_heartbeat(task_id: UUID, lock_token: UUID):
        async def loop() -> None:
            while True:
                await asyncio.sleep(max(1, settings.task_lease_seconds // 3))
                async with get_session_factory()() as heartbeat_db:
                    if not await TaskService.heartbeat(heartbeat_db, task_id=task_id, lock_token=lock_token):
                        return
        heartbeat_task = asyncio.create_task(loop())
        try:
            yield
        finally:
            heartbeat_task.cancel()
            finished, _ = await asyncio.wait(
                {heartbeat_task},
                timeout=settings.agent_run_turn_cancel_grace_seconds,
            )
            if heartbeat_task not in finished:
                return
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    @staticmethod
    async def reconcile_stale(db: AsyncSession, *, now: datetime | None = None, batch_size: int = 100) -> list[UUID]:
        cutoff = now or utc_now()
        tasks = list((await db.scalars(
            select(TaskModel).where(
                TaskModel.status.in_(TaskService.ACTIVE_STATUSES),
                TaskModel.lease_expires_at.is_not(None),
                TaskModel.lease_expires_at < cutoff,
            ).order_by(TaskModel.lease_expires_at).limit(batch_size).with_for_update(skip_locked=True)
        )).all())
        for task in tasks:
            # Reconciler가 Task만 종료해 실행 이력이 running으로 남는 모순을 방지합니다.
            run = await db.scalar(
                select(AgentRunModel).where(
                    AgentRunModel.task_id == task.task_id,
                    AgentRunModel.status == AgentRunStatus.RUNNING,
                ).order_by(AgentRunModel.created_at.desc()).with_for_update()
            )
            if run is not None:
                if run.attempt_count <= max(0, settings.agent_worker_max_retries):
                    # Worker/Pod가 사라져 lease가 만료돼도 남은 횟수가 있으면 durable queue로 복구합니다.
                    delay = TaskService.retry_delay_seconds(run.attempt_count)
                    run.status = AgentRunStatus.PENDING
                    run.failure = {
                        "message": "Task lease expired.",
                        "retry_scheduled": True,
                        "failed_attempt": run.attempt_count,
                    }
                    run.completed_at = None
                    run.next_attempt_at = cutoff + timedelta(seconds=delay)
                    task.status = TaskStatus.PENDING
                    task.failure_reason = "Task lease expired; retry scheduled."
                    task.lock_owner = "retry:reconciler"
                    task.lock_token = None
                    task.heartbeat_at = None
                    task.lease_expires_at = None
                    # 순환 import를 피하면서 기존 원자 sequence Event Store를 그대로 사용합니다.
                    from app.services.task_event_service import TaskEventService

                    await TaskEventService.append_for_run(
                        db,
                        run_id=run.run_id,
                        event_type="task.retry_scheduled",
                        payload={
                            "status": "pending",
                            "reason": "lease_expired",
                            "failed_attempt": run.attempt_count,
                            "max_retries": max(0, settings.agent_worker_max_retries),
                            "delay_seconds": delay,
                            "next_attempt_at": run.next_attempt_at.isoformat(),
                            "failure": run.failure,
                        },
                        commit=False,
                    )
                    continue
                run.status = AgentRunStatus.TIMEOUT
                run.failure = {"message": "Task lease expired; retries exhausted."}
                run.completed_at = cutoff
            TaskService.transition(task, TaskStatus.TIMEOUT, failure_reason="Task lease expired.")
        await db.commit()
        return [task.task_id for task in tasks]
