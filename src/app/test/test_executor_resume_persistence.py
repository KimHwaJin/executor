"""Worker checkpoint recovery, CRUD terminal projection and SSE draining."""
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from app.agent_worker.langgraph_adapter import LangGraphEventAdapter
from app.core.enums import AgentRunStatus, TaskStatus
from app.services.executor_resume_persistence import execution_outcome, project_executor_resume
from app.services.graph_event_persistence import GraphPersistenceContext, GraphPersistenceCursor, extract_graph_events
from app.services.task_service import TaskService
from app.services.workflow_task_stream import task_stream_response
from app.worker.contracts import DeferEvent, EventContext, ExecutorEvent


class ProjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        execution, graph_task, session = uuid4(), uuid4(), uuid4()
        self.context = EventContext(namespace="test", tenant="dtest", session_id=str(session), task_id=str(graph_task),
            execution_id=execution, command_id=uuid4(), event=ExecutorEvent(
                event_id=uuid4(), execution_id=execution, event_type="execution.completed", event_sequence=6,
                schema_version="1.0", occurred_at="2026-10-08T00:00:00Z", payload={"status": "FAILED"}))
        self.run = NS(run_id=uuid4(), session_id=session, trigger_message_id=None, status=AgentRunStatus.RUNNING,
            metadata_json={"executor_execution_id": str(execution), "executor_message_count": 1},
            agent_response={}, interrupt=[], failure=None, completed_at=None)
        self.task = NS(status=TaskStatus.RUNNING, lock_owner=TaskService.EXECUTOR_OWNER, completed_at=None)
        self.state = {"session_id": str(session), "task_id": str(graph_task), "execution_id": str(execution),
            "execution_status": "FAILED", "final_response": {"message": "분석 실행에 실패했습니다. 원인: HTTP 404"},
            "messages": [{"role": "agent", "content": "제출했습니다"},
                         {"role": "assistant", "name": "executor_result", "content": "실패: HTTP 404"}]}
        self.snapshot = NS(values=self.state, next=(), tasks=[])
        self.db = NS(scalar=AsyncMock(return_value=self.run), get=AsyncMock(return_value=NS(user_id=uuid4())),
                     commit=AsyncMock())
        self.persist = AsyncMock()
        self.append = AsyncMock()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = "app.services.executor_resume_persistence."
        self.stack.enter_context(patch(root + "GraphPersistenceDispatcher.default", return_value=NS(persist_state_delta=self.persist)))
        self.stack.enter_context(patch(root + "RunService._lock_run_and_task", AsyncMock(return_value=(self.run, self.task))))
        self.stack.enter_context(patch(root + "TaskEventService.append_for_run", self.append))

    async def test_failed_execution_persists_messages_before_terminal_event_and_releases_lock(self):
        order = []
        self.persist.side_effect = lambda *a, **kw: order.append("messages")
        self.append.side_effect = lambda *a, **kw: order.append("task")
        await project_executor_resume(self.db, self.context, self.snapshot)
        self.assertEqual(order, ["messages", "task"])
        self.assertEqual(self.run.status, AgentRunStatus.ERROR)
        self.assertEqual(self.task.status, TaskStatus.ERROR)
        self.assertIsNone(self.task.lock_owner)
        self.assertIsNotNone(self.task.completed_at)
        self.assertIn("404", self.run.failure["message"])
        self.assertEqual(self.append.call_args.kwargs["event_type"], "task.error")
        self.assertFalse(self.append.call_args.kwargs["commit"])
        self.assertEqual(self.persist.call_args.args[2].message_count, 1)
        await project_executor_resume(self.db, self.context, self.snapshot)
        self.persist.assert_awaited_once()
        self.append.assert_awaited_once()

    async def test_multi_intermediate_result_keeps_waiting(self):
        self.state["execution_status"] = "WAITING_FOR_OPERATION"
        self.snapshot.next = ("wait_executor_event",)
        self.snapshot.tasks = [NS(interrupts=[NS(value={"kind": "EXECUTOR_EVENT"})])]
        await project_executor_resume(self.db, self.context, self.snapshot)
        self.assertEqual(self.task.status, TaskStatus.RUNNING)
        self.assertEqual(self.task.lock_owner, TaskService.EXECUTOR_OWNER)
        self.assertIsNone(self.task.completed_at)
        self.append.assert_not_awaited()

    async def test_success_and_cancellation(self):
        for status, expected in [("SUCCEEDED", "success"), ("CANCELLED", "canceled")]:
            with self.subTest(status=status):
                self.run.status = AgentRunStatus.RUNNING
                self.run.metadata_json.pop("executor_projected_command_id", None)
                self.state["execution_status"] = status
                await project_executor_resume(self.db, self.context, self.snapshot)
                self.assertEqual(self.task.status.value, expected)
                self.assertEqual(self.append.call_args.kwargs["event_type"], "task." + expected)

    async def test_publication_failure_is_retryable_after_checkpoint_completion(self):
        self.state["ew_receipts"] = {str(self.context.command_id): str(self.context.event.event_id)}
        graph = NS(aget_state=AsyncMock(return_value=self.snapshot), ainvoke=AsyncMock())
        publish = AsyncMock(side_effect=[RuntimeError("database unavailable"), None])
        adapter = LangGraphEventAdapter(graph, persist_result=publish)
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            await adapter(self.context)
        await adapter(self.context)
        self.assertEqual(publish.await_count, 2)
        graph.ainvoke.assert_not_awaited()

    async def test_handoff_race_defers_without_losing_receipt(self):
        self.db.scalar.return_value = None
        with self.assertRaises(DeferEvent):
            await project_executor_resume(self.db, self.context, self.snapshot)
        self.persist.assert_not_awaited()

    async def test_unfinished_graph_does_not_publish_terminal_state(self):
        self.snapshot.next = ("generate_report",)
        with self.assertRaises(DeferEvent):
            await project_executor_resume(self.db, self.context, self.snapshot)
        self.persist.assert_not_awaited()

    async def test_old_execution_cannot_finish_new_task(self):
        self.state["execution_id"] = str(uuid4())
        await project_executor_resume(self.db, self.context, self.snapshot)
        self.db.scalar.assert_not_awaited()

    def test_collection_failure_is_error_even_when_executor_succeeded(self):
        status, failure = execution_outcome({"execution_status": "SUCCEEDED", "result_collection_error": "missing manifest"})
        self.assertEqual(status, AgentRunStatus.ERROR)
        self.assertEqual(failure["execution_status"], "SUCCEEDED")
        self.assertEqual(failure["code"], "EXECUTOR_RESULT_COLLECTION_FAILED")

    def test_failed_step_is_not_task_success(self):
        status, _ = execution_outcome({"execution_status": "SUCCEEDED", "executor_result_history": [{"result": {"status": "FAILED"}}]})
        self.assertEqual(status, AgentRunStatus.ERROR)

    def test_failure_message_projects_as_final_assistant_message(self):
        events, _ = extract_graph_events(self.state, GraphPersistenceCursor(message_count=1),
            context=GraphPersistenceContext(user_id=uuid4(), session_id=self.state["session_id"]))
        messages = [event.payload for event in events if event.target == "messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "assistant")
        self.assertIn("404", messages[0]["content"])


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_events_drain_before_snapshot_and_close(self):
        task_id, session_id, run_id = uuid4(), uuid4(), uuid4()
        snapshot = NS(task_id=task_id, session_id=session_id, current_run_id=run_id,
            last_event_sequence=2, task_finished=True, status=TaskStatus.ERROR,
            model_dump=Mock(return_value={"status": "error", "task_finished": True}))
        events = [NS(run_id=run_id, task_event_id=uuid4(), created_at=datetime.now(timezone.utc), sequence=i,
            event_type=kind, payload=payload) for i, kind, payload in [
                (1, "message.completed", {"channel": "final", "content": [{"type": "text", "text": "실행 실패"}]}),
                (2, "task.error", {"status": "error"})]]
        cm = AsyncMock()
        root = "app.services.workflow_task_stream."
        with patch(root + "get_session_factory", return_value=Mock(return_value=cm)), \
             patch(root + "task_snapshot", AsyncMock(return_value=snapshot)), \
             patch(root + "TaskEventService.list_after_task", AsyncMock(return_value=events)):
            response = task_stream_response(NS(is_disconnected=AsyncMock(return_value=False)), uuid4(), task_id, 0)
            frames = [frame async for frame in response.body_iterator]
        self.assertEqual(len(frames), 4)
        for frame, event_type in zip(frames, ["message.completed", "task.error", "task.snapshot", "stream.closed"]):
            self.assertIn("event: " + event_type, frame)
        self.assertIn('"reason":"task_finished"', frames[-1])


if __name__ == "__main__":
    unittest.main()
