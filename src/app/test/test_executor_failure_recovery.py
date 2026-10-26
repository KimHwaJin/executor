"""Executor contract regressions: terminal events, missing artifacts and receipts."""
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import InMemorySaver
from agent_config import load_agent_settings
from app.agent_worker.langgraph_adapter import LangGraphEventAdapter
from app.graphs.builders.build_analysis_workflow_graph import build_analysis_workflow_graph
from app.graphs.nodes.adaptive_executor import make_cancel_adaptive_execution
from app.graphs.nodes.execution_results import make_collect_execution_results
from app.graphs.nodes.generate_report import execution_is_reportable
from app.graphs.routers.orchestration_router import route_after_finalize
from app.services.execution_notebook_reader import read_current_operation_results
from app.worker.contracts import EventContext, ExecutorEvent, IgnoreEvent


EXECUTION = "10000000-0000-0000-0000-000000000001"
OPERATION = "20000000-0000-0000-0000-000000000001"
STEP = "30000000-0000-0000-0000-000000000001"


def settings():
    return load_agent_settings({"EXECUTOR_RESULT_READ_MODE": "MANIFEST", "DEMO_ARTIFACTS_ENABLED": "false"})


def state(mode="SINGLE"):
    return {
        "task_id": str(uuid4()), "session_id": str(uuid4()), "execution_id": EXECUTION,
        "execution_mode": mode, "executor_operation_number": 1,
        "executor_wait_phase": "execution_completed" if mode == "SINGLE" else "operation_completed",
        "execution_status": "RUNNING", "workflow": {},
        "execution_steps": [{"sequence": 0, "lineage": {
            "skill_name": "data_load", "tool_name": "extract_data",
            "input_parameters": {"step_id": "step_0", "tool_id": "step_0_tool_0"},
        }}],
    }


def result_body(status="FAILED", ref=None):
    return {"body": {"operations": [{"operation_id": OPERATION, "operation_number": 1,
        "steps": [{"step_id": STEP, "sequence": 0, "result": {
            "status": status, "error_message": "HTTPError: 404 Not Found" if status == "FAILED" else None,
            "result_ref": ref,
        }}]}]}}


class ManifestFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_event_resolves_result_api_without_notebook_get(self):
        s = state()
        s["execution_event"] = {"event_type": "execution.completed", "status": "FAILED",
                                "response": {"status": "FAILED", "error": {"message": "HTTP 404"}}}
        fetch = AsyncMock(return_value=result_body())
        notebook = Mock(side_effect=AssertionError("Notebook GET must not be used"))
        results = await read_current_operation_results(settings(), s, fetch_result=fetch, fetch_notebook=notebook)
        fetch.assert_awaited_once()
        notebook.assert_not_called()
        self.assertEqual(results[0]["result"]["status"], "FAILED")
        self.assertIn("404", results[0]["result"]["error_message"])
        self.assertEqual(results[0]["result"]["outputs"], [])

    async def test_operation_failure_without_manifest_uses_event_error(self):
        for status in ("FAILED", "CANCELLED"):
            s = state("MULTI")
            s["execution_event"] = {"response": {"operation": {"id": OPERATION, "number": 1},
                "error": {"message": "runtime stopped"}, "step_results": [
                    {"step_id": STEP, "sequence": 0, "status": status, "result_ref": None}]}}
            result = await read_current_operation_results(settings(), s)
            self.assertEqual(result[0]["result"]["status"], status)
            self.assertEqual(result[0]["result"]["error_message"], "runtime stopped")

    async def test_success_without_manifest_remains_invalid(self):
        s = state()
        s["execution_event"] = {"status": "SUCCEEDED", "response": {"status": "SUCCEEDED"}}
        with self.assertRaisesRegex(RuntimeError, "has no result_ref"):
            await read_current_operation_results(settings(), s, fetch_result=Mock(return_value=result_body("SUCCEEDED")))

    async def test_runtime_failure_before_operation_exists(self):
        s = state()
        s["execution_event"] = {"status": "FAILED", "response": {"status": "FAILED"}}
        self.assertEqual(await read_current_operation_results(
            settings(), s, fetch_result=Mock(return_value={"body": {"operations": []}})), [])

    async def test_collection_error_does_not_relabel_executor_success_as_failure(self):
        s = state()
        s["execution_event"] = {"status": "SUCCEEDED", "state_version": 1, "response": {}}
        node = make_collect_execution_results(settings(), adaptive=False,
                                              read_results=Mock(side_effect=FileNotFoundError("missing manifest")))
        with self.assertLogs("app.graphs.nodes.execution_results", level="ERROR"):
            result = await node(s)
        self.assertEqual(result["execution_status"], "SUCCEEDED")
        self.assertIn("missing manifest", result["messages"][0]["content"])
        self.assertFalse(execution_is_reportable(result))

    async def test_cancel_transport_failure_ends_with_honest_notice(self):
        s = {**state("MULTI"), "execution_status": "WAITING_FOR_OPERATION",
             "result_collection_error": "missing manifest"}
        cancel = Mock(side_effect=RuntimeError("cancel unavailable"))
        result = await make_cancel_adaptive_execution(replace(settings(), executor_submit_enabled=True), cancel)(s)
        self.assertEqual(route_after_finalize({**s, **result}), "end")
        self.assertIn("확인하지 못했습니다", result["messages"][0]["content"])


class FailureGraphTests(unittest.IsolatedAsyncioTestCase):
    async def setup_graph(self, mode, reader):
        s = state(mode)
        self.config = {"configurable": {"thread_id": s["session_id"]}}
        self.cancel, self.store = Mock(), Mock()
        self.graph = build_analysis_workflow_graph(
            Mock(), settings(), checkpointer=InMemorySaver(), bindings=Mock(register=AsyncMock()),
            workflow_store=self.store, execution_result_reader=reader, submit_execution_cancel=self.cancel,
        )
        await self.graph.aupdate_state(self.config, s, as_node="build_executor_request")
        result = await self.graph.ainvoke(None, self.config)
        self.assertTrue(result.get("__interrupt__"))
        self.adapter = LangGraphEventAdapter(self.graph)
        return s

    def context(self, s, event_type, payload, sequence=6):
        return EventContext(namespace="test", tenant="dtest", session_id=s["session_id"], task_id=s["task_id"],
            execution_id=UUID(EXECUTION), command_id=uuid4(), event=ExecutorEvent(
                event_id=uuid4(), execution_id=UUID(EXECUTION), event_type=event_type,
                event_sequence=sequence, schema_version="1.0", occurred_at="2026-10-08T00:00:00Z", payload=payload))

    async def assert_ended(self, ctx):
        snapshot = await self.graph.aget_state(self.config)
        self.assertFalse(snapshot.next)
        self.assertEqual(snapshot.values["ew_receipts"][str(ctx.command_id)], str(ctx.event.event_id))
        self.assertEqual(snapshot.values["execution_status"], "FAILED")
        self.assertTrue(any("404" in m["content"] for m in snapshot.values["messages"]))
        self.cancel.assert_not_called()
        self.store.finish_execution.assert_called_once()
        self.assertFalse(self.store.finish_execution.call_args.kwargs["successful"])

    async def test_single_actual_event_shape_ends_and_acknowledges(self):
        fetch = Mock(return_value=result_body())
        async def read(cfg, s):
            return await read_current_operation_results(cfg, s, fetch_result=fetch)
        s = await self.setup_graph("SINGLE", read)
        # The operation event is intentionally not needed by SINGLE.
        with self.assertRaises(IgnoreEvent):
            await self.adapter(self.context(s, "execution.operation_completed", {"status": "FAILED"}, 5))
        ctx = self.context(s, "execution.completed", {"status": "FAILED", "operation_summary": {"total": 1, "failed": 1},
                                                      "retry": None, "error": {"message": "HTTPError 404"}})
        await self.adapter(ctx)
        await self.assert_ended(ctx)
        # Redelivery must not repeat processing.
        await self.adapter(ctx)
        fetch.assert_called_once()

    async def test_single_result_api_failure_still_ends(self):
        s = await self.setup_graph("SINGLE", Mock(side_effect=RuntimeError("result API unavailable")))
        ctx = self.context(s, "execution.completed", {"status": "FAILED", "error": {"message": "HTTPError 404"}})
        with self.assertLogs("app.graphs.nodes.execution_results", level="ERROR"):
            await self.adapter(ctx)
        await self.assert_ended(ctx)

    async def test_single_and_multi_publish_terminal_checkpoint_to_crud_callback(self):
        for mode in ("SINGLE", "MULTI"):
            with self.subTest(mode=mode):
                s = await self.setup_graph(mode, Mock(return_value=[]))
                publish = AsyncMock()
                adapter = LangGraphEventAdapter(self.graph, persist_result=publish)
                ctx = self.context(s, "execution.completed", {
                    "status": "FAILED", "error": {"message": "HTTPError 404"}})
                await adapter(ctx)
                snapshot = publish.call_args.args[1]
                self.assertFalse(snapshot.next)
                self.assertEqual(snapshot.values["execution_status"], "FAILED")
                self.assertTrue(any("404" in m["content"] for m in snapshot.values["messages"]))
                await adapter(ctx)
                self.assertEqual(publish.await_count, 2)
                self.store.finish_execution.assert_called_once()

    async def test_multi_early_terminal_failure_needs_no_operation(self):
        reader = Mock(side_effect=AssertionError("terminal failure has no operation"))
        s = await self.setup_graph("MULTI", reader)
        ctx = self.context(s, "execution.completed", {"status": "FAILED", "error": {"message": "HTTPError 404"}})
        await self.adapter(ctx)
        await self.assert_ended(ctx)
        reader.assert_not_called()

    async def test_multi_failed_operation_ends_without_cancel(self):
        s = await self.setup_graph("MULTI", read_current_operation_results)
        ctx = self.context(s, "execution.operation_completed", {
            "status": "FAILED", "execution_status": "FAILED", "operation": {"id": OPERATION, "number": 1},
            "error": {"message": "HTTPError 404"}, "continuation": None,
            "step_results": [{"step_id": STEP, "sequence": 0, "status": "FAILED", "result_ref": None}],
        }, 5)
        await self.adapter(ctx)
        await self.assert_ended(ctx)
        with self.assertRaises(IgnoreEvent):
            await self.adapter(self.context(s, "execution.completed", {"status": "FAILED"}))

    async def test_multi_failed_operation_with_broken_artifact_still_ends(self):
        s = await self.setup_graph("MULTI", Mock(side_effect=FileNotFoundError("missing manifest")))
        ctx = self.context(s, "execution.operation_completed", {
            "status": "FAILED", "execution_status": "FAILED", "error": {"message": "HTTPError 404"},
            "operation": {"id": OPERATION, "number": 1}, "step_results": [],
        })
        with self.assertLogs("app.graphs.nodes.execution_results", level="ERROR"):
            await self.adapter(ctx)
        await self.assert_ended(ctx)


if __name__ == "__main__":
    unittest.main()
