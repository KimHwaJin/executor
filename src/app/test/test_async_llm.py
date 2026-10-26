"""Native async LLM calls, cancellation and graph boundary regressions."""

import asyncio
import threading
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.constants import CONFIG_KEY_CHECKPOINTER, TAG_NOSTREAM
from pydantic import BaseModel

from agent_config import load_agent_settings
from app.agents.orchestration.agent_interfaces import (
    JsonMessageAgentAdapter, LabelOnlyLLMAgent, SimpleLLMAgent,
    StructuredLLMAgent, ainvoke_typed,
)
from app.agents.orchestration.mock_llm import (
    _FixedAgent, create_mock_dependencies, override_sleep_seconds, track_sleep,
)
from app.agents.report_writer.create_report_generator_agent import MarkdownReportAgent
from app.graphs.builders.build_analysis_workflow_graph import build_analysis_workflow_graph
from app.graphs.nodes.adaptive_execution import make_decide_conditional_tools
from app.graphs.nodes.generate_report import make_generate_report


class Answer(BaseModel):
    answer: str


def async_model(*responses):
    return Mock(invoke=Mock(side_effect=AssertionError("sync LLM call")),
                ainvoke=AsyncMock(side_effect=[AIMessage(content=r) for r in responses]))


class AsyncLLMTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_and_label_adapters_use_async_model(self):
        model = async_model(" hello ", " # report ", '"faq"')
        self.assertEqual(await SimpleLLMAgent(model, "prompt").ainvoke(
            {"user_request": "hello"}), {"answer": "hello"})
        self.assertEqual(await MarkdownReportAgent(model, "prompt").ainvoke({}),
                         {"content": "# report"})
        label = LabelOnlyLLMAgent(model, "prompt", Answer, "answer", ("faq",))
        self.assertEqual((await label.ainvoke({})).answer, "faq")
        model.invoke.assert_not_called()

    async def test_structured_retry_and_exhaustion(self):
        model = async_model('not JSON', '```json\n{"answer":"ok"}\n```')
        agent = StructuredLLMAgent(model, "prompt", Answer)
        self.assertEqual((await ainvoke_typed(agent, {}, Answer)).answer, "ok")
        self.assertEqual(model.ainvoke.await_count, 2)
        self.assertIn("validation", model.ainvoke.call_args.args[0][-1].content)
        bad = StructuredLLMAgent(async_model("{}", "{}"), "prompt", Answer,
                                 max_validation_attempts=2)
        with self.assertRaisesRegex(ValueError, "after 2 attempts"):
            await bad.ainvoke({})

    async def test_provider_schema_uses_async_structured_model(self):
        model = Mock()
        structured = Mock(ainvoke=AsyncMock(return_value=Answer(answer="ok")))
        model.with_structured_output.return_value = structured
        agent = StructuredLLMAgent(model, "prompt", Answer, method="provider_json_schema")
        self.assertEqual((await agent.ainvoke({})).answer, "ok")
        model.with_structured_output.assert_called_once_with(Answer, method="json_schema")
        structured.invoke.assert_not_called()

    async def test_nested_graph_keeps_stream_and_checkpoint_boundaries(self):
        nested = Mock(ainvoke=AsyncMock(return_value={"messages": [
            AIMessage(content=[{"type": "text", "text": '{"answer":"ok"}'}])]}))
        result = await ainvoke_typed(JsonMessageAgentAdapter(nested), {}, Answer)
        self.assertEqual(result.answer, "ok")
        config = nested.ainvoke.call_args.kwargs["config"]
        self.assertIsNone(config["configurable"][CONFIG_KEY_CHECKPOINTER])
        self.assertIn(TAG_NOSTREAM, config["tags"])
        nested.invoke.assert_not_called()

    async def test_sdk_async_transport_overlaps_and_receives_cancellation(self):
        # The real LangChain/OpenAI async stack, with no external service needed.
        entered = asyncio.Event()
        release = asyncio.Event()
        active = 0
        cancelled = 0
        loop_thread = threading.get_ident()

        async def transport(request):
            nonlocal active, cancelled
            self.assertEqual(threading.get_ident(), loop_thread)
            active += 1
            if active == 2:
                entered.set()
            try:
                await release.wait()
                return httpx.Response(200, json={
                    "id": "test", "object": "chat.completion", "created": 1,
                    "model": "test", "choices": [{"index": 0, "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"}}],
                })
            except asyncio.CancelledError:
                cancelled += 1
                raise
            finally:
                active -= 1

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            model = ChatOpenAI(model="test", api_key="test", max_retries=0,
                               http_async_client=client)
            agent = SimpleLLMAgent(model, "prompt")
            tasks = [asyncio.create_task(agent.ainvoke({"user_request": "hello"}))
                     for _ in range(2)]
            try:
                await asyncio.wait_for(entered.wait(), 3)
                tasks[0].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await tasks[0]
                self.assertEqual(cancelled, 1)
                release.set()
                self.assertEqual(await tasks[1], {"answer": "ok"})
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_cancellation_is_not_retried(self):
        model = Mock(ainvoke=AsyncMock(side_effect=asyncio.CancelledError))
        with self.assertRaises(asyncio.CancelledError):
            await StructuredLLMAgent(model, "prompt", Answer).ainvoke({})
        model.ainvoke.assert_awaited_once()

    async def test_mock_waiting_uses_async_sleep_and_tracking(self):
        with override_sleep_seconds(0.01), track_sleep() as elapsed:
            with patch("app.agents.orchestration.mock_llm.time.sleep",
                       side_effect=AssertionError("blocking mock delay")):
                self.assertEqual(await _FixedAgent("ok").ainvoke({}), "ok")
            self.assertEqual(elapsed[0], 0.01)

    async def test_graph_reaches_workflow_selection_without_sync_llm(self):
        deps = create_mock_dependencies()
        for name in ("routing_agent", "analysis_intent_agent", "skill_selector_agent",
                     "workflow_agent"):
            getattr(deps, name).invoke = Mock(side_effect=AssertionError("sync LLM call"))
        settings = replace(load_agent_settings({}), demo_artifacts_enabled=False)
        graph = build_analysis_workflow_graph(deps, settings, checkpointer=InMemorySaver())
        with override_sleep_seconds(0):
            result = await graph.ainvoke(
                {"user_request": "NCE 데이터 품질 확인"},
                {"configurable": {"thread_id": str(uuid4())}})
        self.assertTrue(result["__interrupt__"])
        self.assertEqual(result["workflow_origin"], "generated")

    async def test_conditional_decision_offloads_legacy_store(self):
        deps = replace(create_mock_dependencies(), conditional_decision_agent=Mock(
            ainvoke=AsyncMock(return_value={"decisions": [
                {"tool_id": "conditional", "decision": "include", "reason": "test"}]})))
        loop_thread = threading.get_ident()
        store = Mock()
        store.record_adaptive_round.side_effect = lambda **kw: self.assertNotEqual(
            threading.get_ident(), loop_thread)
        with patch("app.graphs.nodes.adaptive_execution.decision_payload", return_value={
            "candidates": [{"condition_tool_id": "prior", "condition_tool_result": {}}]}), \
             patch("app.graphs.nodes.adaptive_execution.build_notebook", return_value={
                 "workflow": {}, "cells": []}), \
             patch("app.graphs.nodes.adaptive_execution.effective_workflow_snapshot",
                   return_value={}):
            result = await make_decide_conditional_tools(deps, store)({
                "workflow": {"workflow": {"id": "test"}},
                "adaptive_pending_tool_id": "conditional"})
        self.assertEqual(result["adaptive_runtime_decisions"], {"conditional": "include"})
        store.record_adaptive_round.assert_called_once()

    async def test_report_offloads_legacy_artifact_http(self):
        deps = replace(create_mock_dependencies(), report_agent=Mock(
            ainvoke=AsyncMock(return_value={"content": "# Report"})))
        settings = replace(load_agent_settings({}), demo_artifacts_enabled=False,
                           executor_submit_enabled=True, executor_report_source_type="INLINE")
        loop_thread = threading.get_ident()

        def submit(*args):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return {"status_code": 200}

        result = await make_generate_report(deps, settings, submit_artifact=submit)({
            "task_id": "task", "execution_id": "execution", "user_request": "report"})
        self.assertEqual(result["report_status"], "generated")
        deps.report_agent.invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
