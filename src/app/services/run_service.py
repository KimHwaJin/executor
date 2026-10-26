import asyncio
import logging
from datetime import timedelta
from typing import Any, Awaitable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    AgentRunStatus,
    DeleteYN,
    MessageStatus,
    MessageType,
    TaskStatus,
)
from config import settings
from app.core.database import get_session_factory
from app.models.common.agent_run_model import AgentRunModel
from app.models.common.message_model import MessageModel
from app.models.common.task_model import TaskModel
from app.models.common.session_model import SessionModel
from app.repositories.session_repository import SessionRepository
from app.schemas.common.run_schema import RunCancel, RunCreate
from app.services.agent_graph_service import (
    ainvoke_resume,
    ainvoke_user_turn,
    interrupt_payload,
    run_status_from_state,
    user_request_from_messages,
)
from app.services.helpers import utc_now
from app.services.task_service import TaskService
from app.services.task_event_service import TaskEventService
from app.services.llm_token_event_service import LLMTokenEventBuffer
from app.services.workflow_interactions import public_interactions

logger = logging.getLogger(__name__)


class RunService:
    """agent_runs CRUD와 Graph 실행 경계입니다. Message CRUD를 호출하지 않습니다."""

    TERMINAL_STATUSES = {AgentRunStatus.SUCCESS, AgentRunStatus.ERROR, AgentRunStatus.TIMEOUT, AgentRunStatus.CANCELED}

    class CancellationRequested(Exception):
        """내부 Graph task가 명시적인 사용자 취소 요청으로 중단됐음을 표시합니다."""

    class TurnTimedOut(Exception):
        """한 그래프 턴이 상한을 넘겨 워커 슬롯을 반환합니다."""

    @staticmethod
    def _retry_delay_seconds(attempt_count: int) -> float:
        """호환용 진입점이며 실제 정책은 TaskService와 Reconciler가 공유합니다."""
        return TaskService.retry_delay_seconds(attempt_count)

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """사용자 입력/상태 충돌인 4xx는 반복해도 같으므로 자동 재시도하지 않습니다."""
        return not isinstance(exc, HTTPException) or exc.status_code >= 500

    @staticmethod
    async def _wait_for_cancellation(run_id: UUID) -> None:
        """별도 DB session으로 취소 요청을 감시해 다른 API worker의 요청도 감지합니다."""
        interval = max(0.05, settings.task_cancel_poll_interval_seconds)
        while True:
            async with get_session_factory()() as cancellation_db:
                requested_at = await cancellation_db.scalar(
                    select(AgentRunModel.cancel_requested_at).where(
                        AgentRunModel.run_id == run_id
                    )
                )
            if requested_at is not None:
                return
            await asyncio.sleep(interval)

    @staticmethod
    async def _run_cancellable(
        run_id: UUID,
        graph_awaitable: Awaitable[dict[str, Any]],
    ) -> dict[str, Any]:
        """Graph와 DB cancel watcher를 경쟁시켜 실제 coroutine을 cooperative cancel합니다.

        상한을 넘기면 호출을 취소합니다. 취소가 grace 안에 끝나지 않으면
        그 태스크를 더 기다리지 않고 TurnTimedOut을 올립니다.
        """
        graph_task = asyncio.create_task(graph_awaitable)
        cancel_task = asyncio.create_task(RunService._wait_for_cancellation(run_id))
        timeout = settings.agent_run_turn_timeout_seconds
        deadline = asyncio.get_running_loop().time() + timeout
        abandon_graph = False
        try:
            while True:
                if graph_task.done():
                    return RunService._graph_task_result(graph_task)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    abandon_graph = await RunService._abandon_if_unfinished(
                        graph_task,
                        grace=settings.agent_run_turn_cancel_grace_seconds,
                    )
                    if abandon_graph or graph_task.cancelled():
                        raise RunService.TurnTimedOut(
                            f"Graph turn exceeded {timeout:g}s."
                        )
                    return RunService._graph_task_result(graph_task)
                waiters = {graph_task}
                if cancel_task is not None and not cancel_task.done():
                    waiters.add(cancel_task)
                done, _ = await asyncio.wait(
                    waiters,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if graph_task in done:
                    return RunService._graph_task_result(graph_task)
                if cancel_task is not None and cancel_task in done:
                    watcher_error = (
                        None if cancel_task.cancelled() else cancel_task.exception()
                    )
                    if watcher_error is not None:
                        # 감시 조회가 죽어도 그래프 대기에 상한이 없어지면 안 됩니다.
                        logger.warning(
                            "Cancel watcher failed: run_id=%s error=%s",
                            run_id,
                            watcher_error,
                        )
                        cancel_task = None
                        continue
                    abandon_graph = await RunService._abandon_if_unfinished(
                        graph_task,
                        grace=settings.agent_run_turn_cancel_grace_seconds,
                    )
                    raise RunService.CancellationRequested
        finally:
            # 취소가 전달되지 않는 sleep/쿼리를 끝까지 기다리면 상한이 여기서 풀립니다.
            # grace만 기다리고, 끝나지 않은 태스크는 워커를 붙잡지 않게 둡니다.
            leftovers: list[asyncio.Task[Any]] = []
            if not graph_task.done():
                graph_task.cancel()
                leftovers.append(graph_task)
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
                leftovers.append(cancel_task)
            if leftovers:
                await asyncio.wait(
                    leftovers,
                    timeout=settings.agent_run_turn_cancel_grace_seconds,
                )

    @staticmethod
    def _graph_task_result(graph_task: asyncio.Task[dict[str, Any]]) -> dict[str, Any]:
        if graph_task.cancelled():
            raise asyncio.CancelledError
        error = graph_task.exception()
        if error is not None:
            raise error
        return graph_task.result()

    @staticmethod
    async def _abandon_if_unfinished(graph_task: asyncio.Task[Any], *, grace: float) -> bool:
        """취소 후 grace 안에 끝나지 않으면 True. 호출자는 그 태스크를 더 기다리지 않습니다."""
        if not graph_task.done():
            graph_task.cancel()
        finished, _ = await asyncio.wait({graph_task}, timeout=grace)
        return graph_task not in finished

    @staticmethod
    async def _await_or_abandon(awaitable: Awaitable[Any], *, abandon: bool) -> None:
        """정상 경로에서는 끝까지 기다리고, 상한으로 놓은 호출은 grace만 기다립니다."""
        if not abandon:
            await awaitable
            return
        task = asyncio.create_task(awaitable)
        finished, _ = await asyncio.wait(
            {task},
            timeout=settings.agent_run_turn_cancel_grace_seconds,
        )
        if task not in finished:
            task.cancel()

    @staticmethod
    async def _settle_turn_timeout(run_id: UUID, exc: TurnTimedOut) -> None:
        """막힌 요청 세션 대신 새 연결로 재시도 또는 timeout을 기록합니다."""
        failure = {
            "code": "AGENT_RUN_TURN_TIMEOUT",
            "stage": "graph_execution",
            "exception_type": f"{type(exc).__module__}.{type(exc).__name__}",
            "message": str(exc),
        }
        try:
            async with get_session_factory()() as fresh:
                await fresh.execute(text("SET LOCAL lock_timeout = '2s'"))
                run, task = await RunService._lock_run_and_task(fresh, run_id)
                if run.status in RunService.TERMINAL_STATUSES:
                    return
                if (
                    task is not None
                    and run.attempt_count <= max(0, settings.agent_worker_max_retries)
                ):
                    delay = RunService._retry_delay_seconds(run.attempt_count)
                    run.status = AgentRunStatus.PENDING
                    run.failure = {
                        **failure,
                        "retry_scheduled": True,
                        "failed_attempt": run.attempt_count,
                    }
                    run.completed_at = None
                    run.next_attempt_at = utc_now() + timedelta(seconds=delay)
                    task.status = TaskStatus.PENDING
                    task.failure_reason = str(exc)
                    task.lock_owner = "retry:scheduled"
                    task.lock_token = None
                    task.heartbeat_at = None
                    task.lease_expires_at = None
                    await TaskEventService.append_for_run(
                        fresh,
                        run_id=run_id,
                        event_type="task.retry_scheduled",
                        payload={
                            "status": "pending",
                            "reason": "turn_timeout",
                            "failed_attempt": run.attempt_count,
                            "max_retries": max(0, settings.agent_worker_max_retries),
                            "delay_seconds": delay,
                            "next_attempt_at": run.next_attempt_at.isoformat(),
                            "failure": run.failure,
                        },
                        commit=False,
                    )
                    await fresh.commit()
                    return
                RunService._finish_run(run, AgentRunStatus.TIMEOUT, failure=failure)
                if task is not None:
                    TaskService.transition(task, TaskStatus.TIMEOUT, failure_reason=str(exc))
                await RunService._append_error_system_message(
                    fresh,
                    run=run,
                    task=task,
                    error=exc,
                )
                await TaskEventService.append_for_run(
                    fresh,
                    run_id=run_id,
                    event_type="task.timeout",
                    payload={"status": run.status.value, "failure": run.failure},
                    commit=False,
                )
                await fresh.commit()
        except (OperationalError, DBAPIError):
            logger.exception(
                "Turn timeout could not be recorded on a fresh session: run_id=%s",
                run_id,
            )

    @staticmethod
    async def _lock_run_and_task(
        db: AsyncSession,
        run_id: UUID,
    ) -> tuple[AgentRunModel, TaskModel | None]:
        """완료와 취소가 서로 덮어쓰지 않도록 같은 transaction에서 행을 잠급니다."""
        run = await db.scalar(
            select(AgentRunModel)
            .where(AgentRunModel.run_id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if run is None:
            raise RuntimeError(f"Run disappeared during execution: {run_id}")
        task = await db.scalar(
            select(TaskModel)
            .where(TaskModel.task_id == run.task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return run, task

    @staticmethod
    def _finalize_canceled(
        run: AgentRunModel,
        task: TaskModel | None,
    ) -> None:
        """실제 실행 중단을 확인한 뒤 Run/Task를 canceled로 확정하고 잠금을 풉니다."""
        RunService._finish_run(run, AgentRunStatus.CANCELED)
        if task is not None:
            TaskService.transition(task, TaskStatus.CANCELED)

    @staticmethod
    async def _session(db: AsyncSession, user_id: UUID, session_id: UUID):
        session = await SessionRepository.get_active_by_user(db, user_id=user_id, session_id=session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        return session

    @staticmethod
    async def _interrupted_run(db: AsyncSession, session_id: UUID, resume_run_id: UUID | None) -> AgentRunModel:
        conditions = [AgentRunModel.session_id == session_id]
        if resume_run_id is not None:
            conditions.append(AgentRunModel.run_id == resume_run_id)
        else:
            conditions.append(AgentRunModel.status == AgentRunStatus.INTERRUPTED)
        run = await db.scalar(select(AgentRunModel).where(*conditions).order_by(AgentRunModel.created_at.desc()))
        if run is None:
            raise HTTPException(status_code=409, detail="No interrupted run exists to resume in this session.")
        return run

    @staticmethod
    def _finish_run(run: AgentRunModel, status: AgentRunStatus, *, failure: dict | None = None, interrupt=None) -> None:
        """AgentRun 상태는 실행 기록만 변경하며 Task 잠금은 TaskService가 처리합니다."""
        run.status = status
        run.failure = failure
        run.interrupt = interrupt
        if status in RunService.TERMINAL_STATUSES:
            run.completed_at = utc_now()

    @staticmethod
    async def _append_error_system_message(
        db: AsyncSession,
        *,
        run: AgentRunModel,
        task: TaskModel | None,
        error: Exception,
    ) -> None:
        """최종 실행 오류를 채팅 화면에 표시할 system 메시지로 한 번만 저장합니다."""
        client_request_id = uuid5(NAMESPACE_URL, f"dtest-agent:task-error:{run.run_id}")
        existing = await db.scalar(
            select(MessageModel).where(
                MessageModel.session_id == run.session_id,
                MessageModel.client_request_id == client_request_id,
            )
        )
        if existing is not None:
            return

        error_text = str(error)
        message = MessageModel(
            session_id=run.session_id,
            message_type=MessageType.SYSTEM,
            content=[{"type": "text", "text": error_text}],
            content_text=error_text,
            message_status=MessageStatus.FAILED,
            client_request_id=client_request_id,
            error_code="TASK_EXECUTION_ERROR",
            error_message=error_text,
            metadata_json={
                "event": "task.error",
                "run_id": str(run.run_id),
                "task_id": str(task.task_id) if task is not None else None,
            },
            delete_yn=DeleteYN.N,
        )
        db.add(message)
        await db.flush()

        # 새로고침해도 오류가 해당 Session의 마지막 채팅 항목으로 보이게 합니다.
        session = await db.get(SessionModel, run.session_id)
        if session is not None:
            session.current_leaf_message_id = message.message_id

    @staticmethod
    async def create(
        db: AsyncSession,
        user_id: UUID,
        session_id: UUID,
        payload: RunCreate,
        key: str,
        *,
        _execute_existing: bool = False,
    ) -> AgentRunModel:
        """Run을 durable queue에 넣습니다.

        일반 API 호출은 pending Run을 commit하고 즉시 반환합니다. Router lifespan Worker만
        ``_execute_existing=True``로 이미 점유한 Run을 실제 실행합니다.
        """
        session = await RunService._session(db, user_id, session_id)
        previous = await db.scalar(select(AgentRunModel).where(
            AgentRunModel.session_id == session_id, AgentRunModel.idempotency_key == key
        ))
        if previous is not None:
            if not _execute_existing:
                return previous
            run = previous
            task = await db.get(TaskModel, run.task_id)
            if task is None:
                raise RuntimeError(f"Queued Run has no Task: {run.run_id}")
            origin = None
            if payload.command is not None:
                resume_id = payload.metadata.get("resume_run_id") or payload.metadata.get("checkpoint_run_id")
                origin = await RunService._interrupted_run(
                    db, session_id, UUID(str(resume_id)) if resume_id else None
                )
        else:
            owner = "queue:unclaimed"
            origin: AgentRunModel | None = None
            try:
                if payload.command is None:
                    # Queue 대기 중에도 동일 Session의 두 분석 요청이 들어오지 못하게 Task를 선점합니다.
                    task = TaskService.create_model(
                        session_id=session_id, idempotency_key=key, owner=owner
                    )
                    task.status = TaskStatus.PENDING
                    db.add(task)
                    await db.flush()
                else:
                    resume_id = payload.metadata.get("resume_run_id") or payload.metadata.get("checkpoint_run_id")
                    origin = await RunService._interrupted_run(
                        db, session_id, UUID(str(resume_id)) if resume_id else None
                    )
                    if origin.status != AgentRunStatus.INTERRUPTED:
                        raise HTTPException(409, "Run is not waiting for input.")
                    if origin.task_id is not None:
                        latest_id = await db.scalar(select(AgentRunModel.run_id).where(
                            AgentRunModel.task_id == origin.task_id,
                        ).order_by(AgentRunModel.created_at.desc(), AgentRunModel.run_id.desc()).limit(1))
                        if latest_id != origin.run_id:
                            raise HTTPException(409, "Run is stale; refresh the Task before resuming.")
                    if any(item.get("kind") == "EXECUTOR_EVENT" for item in origin.interrupt or []):
                        raise HTTPException(409, "Executor is running; wait for the final report.")
                    if origin.task_id is None:
                        # FAQ 등 Task 없는 checkpoint 재개도 durable queue를 거칩니다.
                        task = TaskService.create_model(
                            session_id=session_id, idempotency_key=key, owner=owner
                        )
                        task.status = TaskStatus.PENDING
                        task.checkpoint_run_id = origin.checkpoint_run_id or origin.run_id
                        db.add(task)
                        await db.flush()
                    else:
                        task = await db.scalar(
                            select(TaskModel).where(TaskModel.task_id == origin.task_id).with_for_update()
                        )
                        if task is None or task.status != TaskStatus.WAITING_INPUT:
                            raise HTTPException(status_code=409, detail="Task is not waiting for input.")
                        if task.cancel_requested_at is not None:
                            raise HTTPException(status_code=409, detail="Task cancellation has been requested.")
                        # 재개 요청도 먼저 pending으로 원자 전환하고 Worker가 실제 lease를 획득합니다.
                        task.status = TaskStatus.PENDING
                        task.lock_owner = owner
                        task.lock_token = None
                        task.heartbeat_at = None
                        task.lease_expires_at = None
            except IntegrityError as exc:
                await db.rollback()
                duplicate = await db.scalar(select(AgentRunModel).where(
                    AgentRunModel.session_id == session_id, AgentRunModel.idempotency_key == key
                ))
                if duplicate is not None:
                    return duplicate
                raise HTTPException(status_code=409, detail="An active task already exists for this session.") from exc

            checkpoint_run_id = task.checkpoint_run_id or (origin.checkpoint_run_id if origin else None)
            metadata = dict(payload.metadata)
            metadata["requested_by_user_id"] = str(user_id)
            if checkpoint_run_id is not None:
                metadata["checkpoint_run_id"] = str(checkpoint_run_id)
            run = AgentRunModel(
                session_id=session_id, task_id=task.task_id,
                status=AgentRunStatus.PENDING,
                input_json=payload.input.model_dump(mode="json") if payload.input else None,
                command_json=payload.command, metadata_json=metadata,
                idempotency_key=key, multitask_strategy=payload.multitask_strategy,
                stream_mode=payload.stream_mode, stream_resumable=payload.stream_resumable,
                on_disconnect=payload.on_disconnect,
            )
            db.add(run)
            await db.flush()
            # 화면/API coordinator가 먼저 저장한 원본 User Message를 Run과 분석 Task에 연결한다.
            # Message CRUD는 자기 테이블만 처리하고, 관계 연결은 실행 경계인 RunService가 담당한다.
            trigger_message_id = metadata.get("trigger_message_id")
            if trigger_message_id is not None:
                try:
                    trigger_message_uuid = UUID(str(trigger_message_id))
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail="Invalid trigger_message_id.") from exc
                trigger_message = await db.scalar(
                    select(MessageModel).where(
                        MessageModel.message_id == trigger_message_uuid,
                        MessageModel.session_id == session_id,
                    )
                )
                if trigger_message is None:
                    raise HTTPException(status_code=422, detail="Trigger message does not belong to this session.")
                run.trigger_message_id = trigger_message_uuid
                task.trigger_message_id = trigger_message_uuid
            if task.root_run_id is None:
                task.root_run_id = run.run_id
            if task.checkpoint_run_id is None:
                task.checkpoint_run_id = run.run_id
                run.metadata_json = {**(run.metadata_json or {}), "checkpoint_run_id": str(run.run_id)}
            if origin is not None:
                for interaction in public_interactions(origin):
                    await TaskEventService.append(
                        db, task_id=task.task_id, run_id=run.run_id,
                        event_type="interaction.submitted",
                        payload={"interaction_id": interaction["interaction_id"],
                                 "revision": interaction["revision"], "status": "submitted",
                                 "source_run_id": str(origin.run_id), "run_id": str(run.run_id)},
                        commit=False,
                    )
            # Worker가 pending Run을 보기 전에 queued event까지 같은 transaction에서 완성한다.
            # commit 뒤 이벤트를 추가하면 Worker의 Task lease UPDATE와 lock 순서가 엇갈려
            # PostgreSQL deadlock이 발생할 수 있다.
            await TaskEventService.append(
                db,
                task_id=task.task_id,
                run_id=run.run_id,
                event_type="task.queued",
                payload={"status": AgentRunStatus.PENDING.value},
                commit=False,
            )
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                duplicate = await db.scalar(select(AgentRunModel).where(
                    AgentRunModel.session_id == session_id, AgentRunModel.idempotency_key == key
                ))
                if duplicate is not None:
                    return duplicate
                raise HTTPException(status_code=409, detail="An active task already exists for this session.") from exc
            await db.refresh(run)
            await db.refresh(task)
            return run

        await db.refresh(run)
        await db.refresh(task)
        # rollback은 ORM 속성을 expire하므로 취소/오류 처리에 쓸 ID는 평범한 값으로 보존합니다.
        execution_run_id = run.run_id
        await TaskEventService.append(
            db,
            task_id=task.task_id,
            run_id=execution_run_id,
            event_type="task.started" if payload.command is None else "task.resumed",
            payload={"status": TaskStatus.RUNNING.value},
        )

        token_events = LLMTokenEventBuffer(task_id=task.task_id, run_id=execution_run_id)
        token_events.start()
        timed_out: RunService.TurnTimedOut | None = None
        run_metadata = dict(run.metadata_json or {})
        mock_llm = bool(run_metadata.get("dtest_mock_llm"))
        mock_llm_sleep_seconds = run_metadata.get("dtest_mock_llm_sleep_seconds")
        try:
            try:
                async with TaskService.lease_heartbeat(task.task_id, task.lock_token):
                    if payload.command is not None:
                        state = await RunService._run_cancellable(
                            execution_run_id,
                            ainvoke_resume(
                                db, user_id=user_id, session_id=session_id,
                                checkpoint_run_id=task.checkpoint_run_id,
                                agent_run_id=execution_run_id,
                                command=payload.command,
                                callbacks=[token_events],
                                mock_llm=mock_llm,
                                mock_llm_sleep_seconds=mock_llm_sleep_seconds,
                            ),
                        )
                    else:
                        if payload.input is None:
                            raise HTTPException(status_code=422, detail="Run input is required when command is omitted.")
                        state = await RunService._run_cancellable(
                            execution_run_id,
                            ainvoke_user_turn(
                                db, user_id=user_id, project_id=session.project_id,
                                session_id=session_id, run_id=run.run_id,
                                user_request=user_request_from_messages(payload.input.messages),
                                trigger_message_id=run.trigger_message_id,
                                callbacks=[token_events],
                                mock_llm=mock_llm,
                                mock_llm_sleep_seconds=mock_llm_sleep_seconds,
                            ),
                        )
            except RunService.TurnTimedOut as exc:
                timed_out = exc
            finally:
                # 마지막 짧은 token buffer까지 terminal event 전에 durable store로 flush합니다.
                # 상한으로 호출을 놓은 경우에는 이 flush가 워커를 다시 붙잡지 않게 합니다.
                await RunService._await_or_abandon(
                    token_events.close(),
                    abandon=timed_out is not None,
                )
            if timed_out is not None:
                await RunService._settle_turn_timeout(execution_run_id, timed_out)
                return run
        except RunService.CancellationRequested:
            # 취소 API가 기록한 요청을 다시 읽은 후에만 terminal 상태와 lock을 변경합니다.
            await db.rollback()
            run, task = await RunService._lock_run_and_task(db, execution_run_id)
            RunService._finalize_canceled(run, task)
            await TaskEventService.append_for_run(
                db, run_id=execution_run_id, event_type="task.canceled",
                payload={"status": "canceled"}, commit=False,
            )
            await db.commit()
            await db.refresh(run)
            return run
        except Exception as exc:
            error_id = str(uuid4())
            # Preserve the original traceback even if database recovery fails.
            logger.exception(
                "Agent run failed: error_id=%s run_id=%s session_id=%s",
                error_id,
                execution_run_id,
                session_id,
            )
            failure = {
                "code": "AGENT_RUN_FAILED",
                "stage": "graph_execution",
                "exception_type": f"{type(exc).__module__}.{type(exc).__name__}",
                "message": str(exc),
                "error_id": error_id,
            }
            await db.rollback()
            run, task = await RunService._lock_run_and_task(db, execution_run_id)
            if run.cancel_requested_at is not None:
                # 취소와 Graph 예외가 경쟁하면 사용자가 먼저 요청한 취소를 최종 상태로 보존합니다.
                RunService._finalize_canceled(run, task)
            elif (
                task is not None
                and RunService._is_retryable(exc)
                and run.attempt_count <= max(0, settings.agent_worker_max_retries)
            ):
                # Task는 terminal로 만들지 않아 동일 Session의 원자 잠금을 계속 유지합니다.
                delay = RunService._retry_delay_seconds(run.attempt_count)
                run.status = AgentRunStatus.PENDING
                run.failure = {
                    **failure,
                    "retry_scheduled": True,
                    "failed_attempt": run.attempt_count,
                }
                run.completed_at = None
                run.next_attempt_at = utc_now() + timedelta(seconds=delay)
                task.status = TaskStatus.PENDING
                task.failure_reason = str(exc)
                task.lock_owner = "retry:scheduled"
                task.lock_token = None
                task.heartbeat_at = None
                task.lease_expires_at = None
                await TaskEventService.append_for_run(
                    db,
                    run_id=execution_run_id,
                    event_type="task.retry_scheduled",
                    payload={
                        "status": "pending",
                        "failed_attempt": run.attempt_count,
                        "max_retries": max(0, settings.agent_worker_max_retries),
                        "delay_seconds": delay,
                        "next_attempt_at": run.next_attempt_at.isoformat(),
                        "failure": run.failure,
                    },
                    commit=False,
                )
                await db.commit()
                await db.refresh(run)
                return run
            else:
                RunService._finish_run(run, AgentRunStatus.ERROR, failure=failure)
                if task is not None:
                    TaskService.transition(task, TaskStatus.ERROR, failure_reason=str(exc))
                # SSE task.error와 같은 오류를 messages에도 system 역할로 영속화합니다.
                await RunService._append_error_system_message(
                    db,
                    run=run,
                    task=task,
                    error=exc,
                )
            await TaskEventService.append_for_run(
                db, run_id=execution_run_id, event_type=f"task.{run.status.value}",
                payload={"status": run.status.value, "failure": run.failure}, commit=False,
            )
            await db.commit()
            if run.status == AgentRunStatus.CANCELED:
                await db.refresh(run)
                return run
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(status_code=503 if isinstance(exc, RuntimeError) else 502, detail=f"Agent graph failed: {exc}") from exc

        executor_wait = any(item.get("kind") == "EXECUTOR_EVENT" for item in interrupt_payload(state) or [])
        status = AgentRunStatus.RUNNING if executor_wait else run_status_from_state(state)
        route = (state.get("routing_result") or {}).get("route")
        # 완료 직전 row lock을 획득해 동시에 들어온 cancel 요청과 최종 상태를 직렬화합니다.
        run, task = await RunService._lock_run_and_task(db, execution_run_id)
        if run.cancel_requested_at is not None:
            RunService._finalize_canceled(run, task)
            await TaskEventService.append_for_run(
                db, run_id=execution_run_id, event_type="task.canceled",
                payload={"status": "canceled"}, commit=False,
            )
            await db.commit()
            await db.refresh(run)
            return run
        RunService._finish_run(run, status, interrupt=interrupt_payload(state))
        if executor_wait:
            run.metadata_json = {
                **(run.metadata_json or {}),
                "executor_execution_id": state.get("execution_id"),
                "executor_message_count": len(state.get("messages") or []),
            }
        load_metadata = dict(run.metadata_json or {})
        slept = state.get("mock_llm_sleep_seconds")
        if slept:
            load_metadata["mock_llm_sleep_seconds"] = slept
        timings = state.get("node_timings")
        if isinstance(timings, dict):
            load_metadata["node_timings"] = timings
        run.metadata_json = load_metadata

        run.agent_response = {
            "route": route,
            "final_response": state.get("final_response"),
            "service_response": state.get("service_response"),
            # E13: 기존 추천 자산 재사용과 LLM 신규 생성을 영속적으로 구분합니다.
            "workflow_origin": state.get("workflow_origin"),
            # recommended 실행은 어떤 전역 template에서 시작했는지도 역추적합니다.
            "selected_workflow_id": (
                (state.get("recommendation") or {}).get("recommendation") or {}
            ).get("workflow_id"),
        }
        if route == "analysis":
            task.trigger_type = "analysis"
            if task is not None:
                task_status = (
                    TaskStatus.WAITING_INPUT
                    if status == AgentRunStatus.INTERRUPTED
                    else TaskStatus(status.value)
                )
                if executor_wait:
                    TaskService.wait_for_executor(task)
                else:
                    TaskService.transition(task, task_status)
        else:
            # A conversational continuation prompt belongs to the Session, not
            # to this completed request. Preserve the Task and its event history.
            if task is not None:
                task.trigger_type = route or "conversation"
                pending = run.interrupt or []
                continuation_only = bool(pending) and all(
                    [a.get("name") for a in item.get("action_requests", [])] == ["next_user_request"]
                    for item in pending
                )
                if continuation_only:
                    RunService._finish_run(run, AgentRunStatus.SUCCESS)
                task_status = (TaskStatus.WAITING_INPUT if run.status == AgentRunStatus.INTERRUPTED
                               else TaskStatus(run.status.value))
                TaskService.transition(task, task_status)
        if task is not None:
            if run.status == AgentRunStatus.INTERRUPTED:
                for interaction in public_interactions(run):
                    await TaskEventService.append_for_run(
                        db, run_id=run.run_id, event_type="interaction.opened",
                        payload=interaction, commit=False,
                    )
            task_event_status = (
                TaskStatus.WAITING_INPUT.value
                if run.status == AgentRunStatus.INTERRUPTED
                else run.status.value
            )
            await TaskEventService.append_for_run(
                db,
                run_id=run.run_id,
                event_type=f"task.{task_event_status}",
                payload={
                    "status": task_event_status,
                    "run_status": run.status.value,
                    "interrupt": run.interrupt,
                    "failure": run.failure,
                },
                commit=False,
            )
        await db.commit()
        await db.refresh(run)
        return run

    @staticmethod
    async def attach_trigger_message(db: AsyncSession, *, run_id: UUID, message_id: UUID) -> None:
        run = await db.get(AgentRunModel, run_id)
        if run is not None and run.trigger_message_id is None:
            run.trigger_message_id = message_id
            if run.task_id is not None:
                await TaskService.attach_trigger(db, task_id=run.task_id, message_id=message_id)

    @staticmethod
    async def read(db: AsyncSession, user_id: UUID, session_id: UUID, run_id: UUID) -> AgentRunModel:
        await RunService._session(db, user_id, session_id)
        run = await db.scalar(select(AgentRunModel).where(
            AgentRunModel.run_id == run_id, AgentRunModel.session_id == session_id
        ))
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        return run

    @staticmethod
    async def cancel(db: AsyncSession, user_id: UUID, session_id: UUID, run_id: UUID, payload: RunCancel) -> AgentRunModel:
        await RunService._session(db, user_id, session_id)
        run = await db.scalar(
            select(AgentRunModel)
            .where(
                AgentRunModel.run_id == run_id,
                AgentRunModel.session_id == session_id,
            )
            .with_for_update()
        )
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found.")
        if (run.metadata_json or {}).get("executor_execution_id"):
            raise HTTPException(409, "Executor is running; cancellation requires Executor acknowledgement.")
        if run.status in RunService.TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="Run is already terminal.")
        if run.cancel_requested_at is not None:
            return run
        now = utc_now()
        # 실제 Graph가 멈추기 전에는 status와 Task lease를 active로 유지합니다.
        run.cancel_requested_at = now
        run.cancel_reason = payload.reason
        task = await db.scalar(
            select(TaskModel).where(TaskModel.task_id == run.task_id).with_for_update()
        )
        if task is not None:
            task.cancel_requested_at = now
        await db.commit()
        await db.refresh(run)
        return run

    @staticmethod
    async def cancel_task(
        db: AsyncSession, user_id: UUID, task_id: UUID, reason: str | None
    ) -> TaskModel:
        """전체 분석 Job을 취소합니다. waiting_input은 실행 coroutine이 없어 즉시 종료합니다."""
        task = await db.scalar(
            select(TaskModel)
            .join(SessionModel, SessionModel.session_id == TaskModel.session_id)
            .where(TaskModel.task_id == task_id, SessionModel.user_id == user_id)
            .with_for_update()
        )
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")
        if task.lock_owner == TaskService.EXECUTOR_OWNER:
            raise HTTPException(409, "Executor is running; cancellation requires Executor acknowledgement.")
        if task.status in TaskService.TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail="Task is already terminal.")

        latest_run = await db.scalar(
            select(AgentRunModel)
            .where(AgentRunModel.task_id == task_id)
            .order_by(AgentRunModel.created_at.desc())
            .with_for_update()
        )
        if task.status == TaskStatus.WAITING_INPUT or latest_run is None:
            TaskService.transition(task, TaskStatus.CANCELED)
            task.cancel_requested_at = utc_now()
            if latest_run is not None:
                await TaskEventService.append(
                    db, task_id=task_id, run_id=latest_run.run_id,
                    event_type="task.canceled",
                    payload={"status": TaskStatus.CANCELED.value, "reason": reason},
                    commit=False,
                )
            await db.commit()
            await db.refresh(task)
            return task

        now = utc_now()
        task.cancel_requested_at = now
        latest_run.cancel_requested_at = now
        latest_run.cancel_reason = reason
        await db.commit()
        await db.refresh(task)
        return task
