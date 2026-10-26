"""Consistent frontend snapshots from Task and its latest Run in one SQL statement."""
from fastapi import HTTPException
from sqlalchemy import select

from app.core.enums import DeleteYN, TaskStatus
from app.models import AgentRunModel, SessionModel, TaskModel
from app.schemas.common.run_schema import RunResource
from app.schemas.common.workflow_task_schema import TaskSnapshot
from app.services.task_service import TaskService
from app.services.workflow_interactions import public_interactions


def build_snapshot(task, run, started_at=None):
    finished = task.status in TaskService.TERMINAL_STATUSES
    waiting = task.status == TaskStatus.WAITING_INPUT and task.cancel_requested_at is None
    items = public_interactions(run) if waiting and run is not None else []
    failure = run.failure if run else None
    retrying = task.status == TaskStatus.PENDING and bool((failure or {}).get("retry_scheduled"))
    failed = task.status in {TaskStatus.ERROR, TaskStatus.TIMEOUT}
    phase = ("executor_wait" if task.lock_owner == TaskService.EXECUTOR_OWNER
             else items[0]["kind"] if items else task.status.value)
    response = getattr(run, "agent_response", None) if run else None
    return TaskSnapshot(
        task_id=task.task_id, session_id=task.session_id,
        current_run_id=run.run_id if run else None,
        status=task.status, phase=phase, last_event_sequence=task.last_event_sequence,
        input_required=bool(items), task_finished=finished,
        current_run=RunResource.model_validate(run) if run else None,
        interactions=items,
        capabilities={"can_resume": bool(items), "can_cancel": not finished
                      and task.cancel_requested_at is None and task.lock_owner != TaskService.EXECUTOR_OWNER},
        result=response if task.status == TaskStatus.SUCCESS else None,
        failure=failure if failed or retrying else None,
        recovery={"status": "retry_scheduled" if retrying else "failed" if failed else "none",
                  "next_attempt_at": run.next_attempt_at if retrying else None,
                  "user_action_required": failed},
        created_at=task.created_at, updated_at=task.updated_at,
        started_at=started_at, completed_at=task.completed_at,
    )


async def task_snapshot(db, user_id, task_id):
    latest_id = select(AgentRunModel.run_id).where(
        AgentRunModel.task_id == TaskModel.task_id,
    ).order_by(AgentRunModel.created_at.desc(), AgentRunModel.run_id.desc()).limit(1).correlate(TaskModel).scalar_subquery()
    first_started = select(AgentRunModel.started_at).where(
        AgentRunModel.task_id == TaskModel.task_id, AgentRunModel.started_at.is_not(None),
    ).order_by(AgentRunModel.started_at).limit(1).correlate(TaskModel).scalar_subquery()
    row = (await db.execute(select(TaskModel, AgentRunModel, first_started).join(
        SessionModel, SessionModel.session_id == TaskModel.session_id,
    ).outerjoin(AgentRunModel, AgentRunModel.run_id == latest_id).where(
        TaskModel.task_id == task_id, SessionModel.user_id == user_id,
        SessionModel.delete_yn == DeleteYN.N,
    ).execution_options(populate_existing=True))).first()
    if row is None:
        raise HTTPException(404, "Task not found.")
    return build_snapshot(*row)
