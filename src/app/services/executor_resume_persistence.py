"""Project checkpointed Executor resumes into the existing chat/Task event store.

The graph receipt and CRUD projection commit separately. Replaying a
receipt must retry this projection, without invoking the completed graph again.
Message persistence already deduplicates by its stable graph identity; the Task
transition and its event commit together after all messages have been saved.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.core.database import get_session_factory
from app.core.enums import AgentRunStatus, TaskStatus
from app.models.common.agent_run_model import AgentRunModel
from app.models.common.session_model import SessionModel
from app.models.common.task_model import TaskModel
from app.services.graph_event_persistence import (
    GraphPersistenceContext, GraphPersistenceCursor, GraphPersistenceDispatcher,
)
from app.services.run_service import RunService
from app.services.task_event_service import TaskEventService
from app.services.task_service import TaskService
from app.worker import DeferEvent, EventContext


def execution_outcome(state: dict[str, Any]) -> tuple[AgentRunStatus, dict | None]:
    """Graph completion alone does not mean that notebook execution succeeded."""
    status = state.get("execution_status")
    response = state.get("final_response") or {}
    collection_error = state.get("result_collection_error")
    failed_steps = any(
        (item.get("result") or {}).get("status") == "FAILED"
        for item in state.get("executor_result_history") or []
    )
    if (status == "FAILED" or collection_error or failed_steps
            or state.get("adaptive_status") == "cancellation_failed"
            or status not in {"SUCCEEDED", "CANCELLED"}):
        return AgentRunStatus.ERROR, {
            "code": "EXECUTOR_RESULT_COLLECTION_FAILED" if collection_error else "EXECUTOR_EXECUTION_FAILED",
            "message": response.get("message") or collection_error or "분석 실행에 실패했습니다.",
            "execution_id": state.get("execution_id"),
            "execution_status": status,
            "errors": response.get("errors") or [],
            "result_collection_error": collection_error or None,
        }
    if status == "CANCELLED":
        return AgentRunStatus.CANCELED, None
    return AgentRunStatus.SUCCESS, None


async def persist_executor_resume(context: EventContext, snapshot: Any) -> None:
    async with get_session_factory()() as db:
        await project_executor_resume(db, context, snapshot)


async def project_executor_resume(db: Any, context: EventContext, snapshot: Any) -> None:
    state = snapshot.values
    if (str(state.get("task_id")) != str(context.task_id)
            or str(state.get("execution_id")) != str(context.execution_id)):
        # An acknowledged old event must never finalize a newer execution.
        return
    run = await db.scalar(
        select(AgentRunModel).join(TaskModel, AgentRunModel.task_id == TaskModel.task_id)
        .where(
            TaskModel.graph_task_id == UUID(str(context.task_id)),
            AgentRunModel.session_id == UUID(str(state["session_id"])),
            AgentRunModel.metadata_json["executor_execution_id"].astext == str(context.execution_id),
        ).order_by(AgentRunModel.created_at.desc()).limit(1)
    )
    if run is None:
        # An event can arrive before the submitting API worker commits its handoff.
        raise DeferEvent("Executor Run handoff has not been persisted yet")
    if run.status in RunService.TERMINAL_STATUSES:
        return
    run_id = run.run_id
    trigger_message_id = run.trigger_message_id
    message_count = int((run.metadata_json or {}).get("executor_message_count", 0))
    session = await db.get(SessionModel, run.session_id)
    if session is None:
        raise RuntimeError("Executor Run session no longer exists")
    interrupts = [item.value for task in snapshot.tasks for item in task.interrupts]
    if snapshot.next and not interrupts:
        raise DeferEvent("Executor graph still needs recovery before publishing its result")
    await GraphPersistenceDispatcher.default().persist_state_delta(
        db, state, GraphPersistenceCursor(message_count=message_count, agent_run_started=True),
        context=GraphPersistenceContext.from_state(
            state, user_id=session.user_id, agent_run_id=run_id,
            trigger_message_id=trigger_message_id,
        ),
    )
    # Message handlers commit independently. Lock and reload only afterwards.
    run, task = await RunService._lock_run_and_task(db, run_id)
    if run.status in RunService.TERMINAL_STATUSES:
        return
    if task is None:
        raise RuntimeError("Executor Run has no CRUD Task")
    metadata = dict(run.metadata_json or {})
    if metadata.get("executor_projected_command_id") == str(context.command_id):
        return
    metadata.update(
        executor_message_count=len(state.get("messages") or []),
        executor_projected_command_id=str(context.command_id),
    )
    run.metadata_json = metadata
    run.agent_response = {
        **(run.agent_response or {}),
        "final_response": state.get("final_response"),
        "service_response": state.get("service_response"),
    }
    if interrupts:
        if all(isinstance(item, dict) and item.get("kind") == "EXECUTOR_EVENT" for item in interrupts):
            RunService._finish_run(run, AgentRunStatus.RUNNING, interrupt=interrupts)
            TaskService.wait_for_executor(task)
            await db.commit()
            return
        # Keep existing HITL representation if the graph requests further input.
        from app.services.workflow_interactions import public_interactions
        RunService._finish_run(run, AgentRunStatus.INTERRUPTED, interrupt=interrupts)
        TaskService.transition(task, TaskStatus.WAITING_INPUT)
        for interaction in public_interactions(run):
            await TaskEventService.append_for_run(
                db, run_id=run_id, event_type="interaction.opened", payload=interaction, commit=False,
            )
        event_status = "waiting_input"
    else:
        status, failure = execution_outcome(state)
        RunService._finish_run(run, status, failure=failure)
        TaskService.transition(task, TaskStatus(status.value), failure_reason=(failure or {}).get("message"))
        event_status = status.value
    await TaskEventService.append_for_run(
        db, run_id=run_id, event_type=f"task.{event_status}",
        payload={"status": event_status, "run_status": run.status.value,
                 "interrupt": run.interrupt, "failure": run.failure}, commit=False,
    )
    await db.commit()
