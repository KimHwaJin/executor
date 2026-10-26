"""Session memory: real graph checkpoints, bounded model inputs, and API deltas."""
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent_config import load_agent_settings, resolve_main_model, with_main_model
from app.context.budget import ContextBudget, ContextBudgetExceeded, estimate_tokens, context_tokenizer, token_chunks
from app.context.builder import ContextBuilder
from app.context.records import snapshot_analysis
from app.agents.orchestration.agent_interfaces import SimpleLLMAgent, StructuredLLMAgent
from app.agents.orchestration.mock_llm import create_mock_dependencies, override_sleep_seconds
from app.graphs.builders.build_analysis_workflow_graph import build_analysis_workflow_graph
from app.graphs.nodes.routing import ensure_analysis_task, receive_request
from app.graphs.nodes.session_context import make_summarize_conversation
from app.schemas.agents.orchestration_schema import RoutingOutput
from app.services.graph_crud_persistence import ainvoke_with_crud_message_persistence, astream_with_crud_message_persistence
from app.services.graph_event_persistence import extract_graph_events


class FixedAgent:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def ainvoke(self, payload):
        self.calls.append(deepcopy(payload))
        return self.result


def settings(**kwargs):
    return replace(load_agent_settings({}), demo_artifacts_enabled=False,
                   workflow_generation_limit=1, **kwargs)


def record_state():
    return {
        "task_id": "analysis-1", "user_request": "NCE 분석", "execution_id": "execution-1",
        "tool_inputs_confirmed": True,
        "execution_inputs": [{"step_index": 0, "tool_index": 0, "parameter": "threshold", "value": 0.9, "confirmed": True}],
        "workflow_id": "wf-1", "workflow_version_id": "v1",
        "workflow_definition": {"workflow": {"name": "EDA", "steps": [
            {"skill": "eda", "tools": [{"tool": "statistics"}, {"tool": "histogram"}]}]}},
        "execution_status": "FAILED",
        "executor_result_history": [
            {"tool_id": "step_0_tool_0", "result": {"status": "FAILED", "error_message": "invalid data"}},
            {"tool_id": "step_0_tool_1", "result": {"status": "NOT_EXECUTED"}},
        ],
        "result_collection_error": "incomplete evidence",
        "analysis_report": {"content": "주요 결과\n한계: 표본 부족"},
        "report_status": "generated",
    }


class RecordTests(unittest.TestCase):
    def test_confirmed_inputs_and_actual_failures_survive_reset(self):
        state = record_state()
        state.update(snapshot_analysis(state))
        original = deepcopy(state)
        new = ensure_analysis_task({**state, "user_request": "WT 분석"})
        record = new["analysis_records"]["analysis-1"]
        self.assertEqual(record["confirmed_inputs"][0]["value"], 0.9)
        self.assertEqual(record["planned_tools"][0]["tool_id"], "step_0_tool_0")
        self.assertEqual(record["tool_results"][1]["result"]["status"], "NOT_EXECUTED")
        self.assertEqual(record["references"]["execution_id"], "execution-1")
        self.assertEqual(new["execution_id"], "")
        self.assertEqual(new["result_collection_error"], "")
        self.assertEqual(new["execution_inputs"], [])
        self.assertNotEqual(new["current_analysis_task_id"], "analysis-1")
        self.assertEqual(state, original)

    def test_unconfirmed_prefill_is_not_confirmed_memory(self):
        state = record_state()
        state["tool_inputs_confirmed"] = False
        record = snapshot_analysis(state)["analysis_records"]["analysis-1"]
        self.assertNotIn("confirmed_inputs", record)

    def test_messages_only_next_turn_overrides_stale_request(self):
        result = receive_request({"user_request": "old", "last_received_message_id": "m1",
            "messages": [{"id": "m1", "role": "user", "content": "old"},
                         {"id": "m2", "role": "user", "content": "new"}]},
            {"configurable": {"thread_id": "s"}})
        self.assertEqual(result["user_request"], "new")
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["last_received_message_id"], "m2")

    def test_same_text_new_api_turn_is_not_deduplicated(self):
        result = receive_request({"user_request": "again", "last_received_message_id": "m1",
            "messages": [{"id": "m1", "role": "user", "content": "again"}]}, {})
        self.assertEqual(len(result["messages"]), 1)
        self.assertNotEqual(result["messages"][0]["id"], "m1")


class BudgetTests(unittest.TestCase):
    def test_fixed_budgets_do_not_change_when_switching_models_or_output(self):
        large = with_main_model(settings(), resolve_main_model({"name": "GaiA-Big-Latest",
            "parameters": {"max_output_tokens": 8192}}))
        small = with_main_model(large,
            resolve_main_model({"name": "GaiA-Small-Latest", "parameters": {"max_output_tokens": 2048}}))
        first, second = ContextBuilder(large), ContextBuilder(small)
        self.assertEqual(vars(first), vars(second))
        self.assertEqual(first.budget.input_limit, 32000)
        self.assertEqual(first.summary_trigger, 8000)
        self.assertEqual(first.summary_target, 1000)
        state = {"messages": [{"role": "user", "content": "한글" * 500},
                              {"role": "assistant", "content": "답변" * 500}]}
        original = deepcopy(state)
        self.assertEqual(first.build(state, "routing", {}), second.build(state, "routing", {}))
        self.assertEqual(state, original)

    def test_fixed_budget_environment_validation(self):
        cfg = load_agent_settings({"CONTEXT_INPUT_BUDGET_TOKENS": "16000", "CONTEXT_SUMMARY_TRIGGER_TOKENS": "4000"})
        builder = ContextBuilder(cfg)
        self.assertEqual((builder.summary_trigger, builder.recent_turns_budget, builder.summary_target), (4000, 2000, 500))
        self.assertEqual((builder.analysis_list_budget, builder.summary_chunk), (1000, 1000))
        for env in ({"CONTEXT_INPUT_BUDGET_TOKENS": "0"}, {"CONTEXT_SUMMARY_TRIGGER_TOKENS": "abc"},
                    {"CONTEXT_SUMMARY_TRIGGER_TOKENS": "100"}, {"CONTEXT_SUMMARY_TRIGGER_TOKENS": "32000"}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                load_agent_settings(env)

    def test_tokenizer_counts_tokens_not_bytes_and_is_offline(self):
        with patch("socket.create_connection", side_effect=AssertionError("network access")):
            context_tokenizer.cache_clear()
            self.assertEqual(estimate_tokens("hello world"), 2)
            self.assertEqual(estimate_tokens(""), 0)
            text = "한글과 emoji 🙂, <|endoftext|>"
            self.assertEqual(estimate_tokens(text), len(context_tokenizer().encode(text, disallowed_special=())))
            self.assertNotEqual(estimate_tokens(text), len(text.encode("utf-8")))

    def test_unsummarized_turns_are_never_silently_removed(self):
        budget = ContextBudget(2200)
        payload = {"user_request": "question", "confirmed_inputs": {"threshold": 0.9},
                   "session_context": {"recent_turns": [[{"content": "token " * 10000}]], "conversation_summary": "old"}}
        with self.assertRaises(ContextBudgetExceeded):
            budget.fit_payload(payload, "system")
        self.assertEqual(payload["confirmed_inputs"], {"threshold": 0.9})
        self.assertTrue(payload["session_context"]["recent_turns"])
        with self.assertRaises(ContextBudgetExceeded):
            budget.fit_payload({"confirmed_inputs": {"data": "token " * 10000}}, "system")
        with self.assertRaises(ContextBudgetExceeded):
            budget.fit_payload({}, "token " * 10000)

    def test_simple_agent_receives_context_and_rejects_truncated_answers(self):
        model = Mock()
        model.invoke.return_value = AIMessage(content="answer")
        agent = SimpleLLMAgent(model, "system", budget=ContextBudget(3200))
        agent.invoke({"user_request": "parameters?", "session_context": {"value": 0.9}})
        self.assertIn('"value": 0.9', model.invoke.call_args.args[0][1].content)
        model.invoke.return_value = AIMessage(content="partial", response_metadata={"finish_reason": "length"})
        with self.assertRaises(ContextBudgetExceeded):
            agent.invoke({"user_request": "parameters?"})

    def test_schema_and_retry_feedback_count_toward_budget(self):
        model = Mock()
        model.invoke.return_value = AIMessage(content="invalid " * 10000)
        agent = StructuredLLMAgent(model, "system", RoutingOutput, budget=ContextBudget(4200))
        with self.assertRaises(ContextBudgetExceeded):
            agent.invoke({"user_request": "hello"})
        self.assertEqual(model.invoke.call_count, 1)

    def test_current_question_once_and_tool_turns_remain_whole(self):
        builder = ContextBuilder(settings())
        state = {"user_request": "now", "messages": [
            {"id": "1", "role": "user", "content": "before"},
            {"id": "2", "role": "assistant", "tool_calls": [{"id": "call"}], "content": ""},
            {"id": "3", "role": "tool", "tool_call_id": "call", "content": "result"},
            {"id": "4", "role": "user", "content": "now"}]}
        payload = builder.build(state, "routing", {"user_request": "now"})
        turns = payload["session_context"]["recent_turns"]
        self.assertEqual(len(turns), 1)
        self.assertEqual([m["id"] for m in turns[0]], ["1", "2", "3"])
        self.assertEqual(json.dumps(payload).count('"now"'), 1)

    def test_report_is_shortened_only_when_required_and_original_is_unchanged(self):
        budget = ContextBudget(3200)
        payload = {"user_request": "explain", "session_context": {
            "analyses": [{"confirmed_inputs": [{"value": 0.9}], "limitations": ["small sample"]}],
            "reports": [{"message_id": "report-1", "content": "evidence " * 5000}]}}
        bounded = budget.fit_payload(payload, "system")
        self.assertTrue(bounded["session_context"]["reports"][0]["truncated"])
        self.assertEqual(bounded["session_context"]["analyses"], payload["session_context"]["analyses"])
        self.assertNotIn("truncated", payload["session_context"]["reports"][0])

    def test_chat_model_sends_configured_output_limit(self):
        from app.agents.orchestration.dependencies import create_chat_model
        cfg = replace(settings(), model_provider="openai_compatible", model_name="gpt-oss-120b",
                      model_api_key="dummy", api_base_url="http://localhost:9999/v1", main_model=resolve_main_model({"name": "GaiA-Big-Latest", "parameters": {"max_output_tokens": 8192}}))
        with patch("langchain_openai.ChatOpenAI") as model:
            create_chat_model(cfg)
        self.assertEqual(model.call_args.kwargs["max_tokens"], 8192)


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_absolute_trigger_and_unicode_chunk_limit(self):
        text = '한글🙂"\\' * 400
        chunks = token_chunks(text, 1000)
        self.assertEqual("".join(chunks), text)
        self.assertNotIn("\ufffd", "".join(chunks))
        self.assertTrue(all(estimate_tokens(chunk) <= 1000 for chunk in chunks))
        state = {"messages": [{"id": "old", "role": "user", "content": text},
                              {"id": "new", "role": "user", "content": "next"}]}
        agent = FixedAgent({"answer": "요약"})
        high = ContextBuilder(settings(context_summary_trigger_tokens=30000))
        deps = replace(create_mock_dependencies(), context_builder=high, summary_agent=agent)
        self.assertEqual(await make_summarize_conversation(deps)(state), {})
        self.assertEqual(agent.calls, [])
        low = ContextBuilder(settings(context_summary_trigger_tokens=2000))
        result = await make_summarize_conversation(replace(deps, context_builder=low))(state)
        self.assertEqual(result["summarized_through_message_id"], "old")
        self.assertTrue(all(estimate_tokens(call["conversation_chunk"]) <= 1000 for call in agent.calls))
        self.assertTrue(all(call["target_tokens"] == low.summary_target for call in agent.calls))
        self.assertEqual(state["messages"][0]["content"], text)

    async def test_large_previous_turn_is_summarized_below_trigger(self):
        builder = ContextBuilder(settings())
        state = {"user_request": "next", "messages": [
            {"id": "u1", "role": "user", "content": "before"},
            {"id": "a1", "role": "assistant", "content": "token " * 5000},
            {"id": "u2", "role": "user", "content": "next"}]}
        self.assertLess(estimate_tokens(state["messages"]), builder.summary_trigger)
        agent = FixedAgent({"answer": "previous decision"})
        deps = replace(create_mock_dependencies(), context_builder=builder, summary_agent=agent)
        result = await make_summarize_conversation(deps)(state)
        self.assertEqual(result["summarized_through_message_id"], "a1")
        context = builder.build({**state, **result}, "routing", {})["session_context"]
        self.assertEqual(context["conversation_summary"], "previous decision")
        self.assertEqual(context["recent_turns"], [])
        self.assertGreater(len(agent.calls), 1)

    async def test_failure_keeps_original_history_within_total_budget(self):
        builder = ContextBuilder(settings())
        state = {"user_request": "next", "messages": [
            {"id": "u1", "role": "user", "content": "before"},
            {"id": "a1", "role": "assistant", "content": "token " * 5000},
            {"id": "u2", "role": "user", "content": "next"}]}
        agent = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("offline")))
        deps = replace(create_mock_dependencies(), context_builder=builder, summary_agent=agent)
        with self.assertLogs("app.graphs.nodes.session_context", level="WARNING"):
            result = await make_summarize_conversation(deps)(state)
        self.assertEqual(result, {})
        payload = builder.build(state, "routing", {"user_request": "next"})
        fitted = builder.budget.fit_payload(payload, "system")
        self.assertEqual(fitted["session_context"]["recent_turns"][0], state["messages"][:2])
        with self.assertRaises(ContextBudgetExceeded):
            ContextBudget(2000).fit_payload(payload, "system")

    async def test_summary_covers_more_than_four_old_turns(self):
        builder = ContextBuilder(settings())
        messages = [{"id": str(i), "role": "user", "content": "token " * 1100} for i in range(9)]
        messages.append({"id": "current", "role": "user", "content": "next"})
        state = {"user_request": "next", "messages": messages}
        agent = FixedAgent({"answer": "summary"})
        deps = replace(create_mock_dependencies(), context_builder=builder, summary_agent=agent)
        result = await make_summarize_conversation(deps)(state)
        self.assertGreater(int(result["summarized_through_message_id"]), 3)
        remaining = builder.recent_turns({**state, **result})
        self.assertLessEqual(estimate_tokens(remaining), builder.recent_turns_budget)
        self.assertEqual(remaining[-1][-1]["id"], "8")

    async def test_chunked_summary_preserves_original_and_advances_at_turn_boundary(self):
        builder = ContextBuilder(settings(context_summary_trigger_tokens=1000))
        agent = FixedAgent({"answer": "분석 결과 확인 중"})
        deps = replace(create_mock_dependencies(), context_builder=builder, summary_agent=agent)
        messages = [{"id": "u1", "role": "user", "content": "old"},
                    {"id": "a1", "role": "assistant", "content": "내용" * 3000},
                    {"id": "u2", "role": "user", "content": "follow up"}]
        state = {"messages": messages}
        original = deepcopy(state)
        result = await make_summarize_conversation(deps)(state)
        self.assertEqual(result["summarized_through_message_id"], "a1")
        self.assertNotIn("messages", result)
        self.assertEqual(state, original)
        self.assertGreater(len(agent.calls), 1)
        for payload in agent.calls:
            self.assertLess(estimate_tokens(payload), builder.budget.input_limit)
        self.assertEqual(builder.unsummarized({**state, **result}), messages[-1:])

    async def test_summary_failure_does_not_advance_cursor(self):
        agent = SimpleNamespace(ainvoke=AsyncMock(side_effect=RuntimeError("timeout")))
        builder = ContextBuilder(settings(context_summary_trigger_tokens=1000))
        deps = replace(create_mock_dependencies(), context_builder=builder, summary_agent=agent)
        state = {"conversation_summary": "existing", "summarized_through_message_id": "previous",
                 "messages": [{"id": "u", "role": "user", "content": "token " * 10000},
                              {"id": "new", "role": "user", "content": "new"}]}
        with self.assertLogs("app.graphs.nodes.session_context", level="WARNING"):
            result = await make_summarize_conversation(deps)(state)
        self.assertEqual(result, {})
        self.assertEqual(state["conversation_summary"], "existing")


class MultiTurnGraphTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sleep = override_sleep_seconds(0)
        self.sleep.__enter__()
        self.addCleanup(self.sleep.__exit__, None, None, None)
        self.router = FixedAgent({"route": "analysis_question", "reason": "follow-up", "analysis_task_ids": ["analysis-1"]})
        self.answer = FixedAgent({"answer": "임계값은 0.9이고 실행은 실패했습니다."})
        self.deps = replace(create_mock_dependencies(), routing_agent=self.router, analysis_question_agent=self.answer)
        self.checkpointer = InMemorySaver()
        self.graph = build_analysis_workflow_graph(self.deps, settings(), checkpointer=self.checkpointer)
        self.config = {"configurable": {"thread_id": str(uuid4())}}
        state = record_state()
        state.update(snapshot_analysis(state))
        state.update(current_analysis_task_id="analysis-1", messages=[
            {"id": "report-analysis-1", "role": "assistant", "content": state["analysis_report"]["content"]}])
        await self.graph.aupdate_state(self.config, state, as_node="generate_report")
        self.assertFalse((await self.graph.aget_state(self.config)).next)

    async def test_end_start_restores_records_and_passes_confirmed_evidence(self):
        # Recompile to prove the memory is in the checkpointer, not in the node object.
        self.graph = build_analysis_workflow_graph(self.deps, settings(), checkpointer=self.checkpointer)
        result = await self.graph.ainvoke({"user_request": "아까 파라미터와 결과는?"}, self.config)
        self.assertNotIn("__interrupt__", result)
        self.assertEqual(result["final_response"]["status"], "analysis_question_answered")
        context = self.answer.calls[0]["session_context"]
        self.assertEqual(context["analyses"][0]["confirmed_inputs"][0]["value"], 0.9)
        self.assertEqual(context["analyses"][0]["tool_results"][0]["result"]["status"], "FAILED")
        self.assertIn("표본 부족", context["reports"][0]["content"])
        self.assertEqual(len(result["messages"]), 3)
        self.assertFalse((await self.graph.aget_state(self.config)).next)

    async def test_new_analysis_resets_execution_but_keeps_old_record(self):
        self.router.result = {"route": "analysis", "reason": "new request"}
        result = await self.graph.ainvoke({"user_request": "WT 분석"}, self.config)
        self.assertIn("__interrupt__", result)
        self.assertNotEqual(result["task_id"], "analysis-1")
        self.assertIn("analysis-1", result["analysis_records"])
        self.assertEqual(result["execution_inputs"], [])
        self.assertEqual(result["execution_id"], "")
        self.assertEqual(result["report_status"], "")
        self.assertEqual(len(result["analysis_records"]), 2)

    async def test_unknown_analysis_asks_for_clarification_without_guessing(self):
        self.router.result["analysis_task_ids"] = ["another-session-analysis"]
        result = await self.graph.ainvoke({"user_request": "다른 분석은?"}, self.config)
        self.assertIn("어떤 분석", result["final_response"]["answer"])
        self.assertEqual(self.answer.calls, [])

    async def test_thread_isolation(self):
        result = await self.graph.ainvoke({"user_request": "아까 결과는?"}, {"configurable": {"thread_id": "different"}})
        self.assertIn("어떤 분석", result["final_response"]["answer"])
        self.assertFalse(result.get("analysis_records"))
        self.assertEqual(self.answer.calls, [])

    async def test_compare_two_analyses_passes_both_and_preserves_current_reference(self):
        snapshot = await self.graph.aget_state(self.config)
        records = deepcopy(snapshot.values["analysis_records"])
        second = deepcopy(records["analysis-1"])
        second.update(task_id="analysis-2", original_request="WT 분석")
        records["analysis-2"] = second
        await self.graph.aupdate_state(self.config, {"analysis_records": records}, as_node="analysis_question")
        self.router.result["analysis_task_ids"] = ["analysis-1", "analysis-2"]
        result = await self.graph.ainvoke({"user_request": "두 분석 비교해줘"}, self.config)
        self.assertEqual([r["task_id"] for r in self.answer.calls[0]["session_context"]["analyses"]],
                         ["analysis-1", "analysis-2"])
        self.assertEqual(result["current_analysis_task_id"], "analysis-1")

    async def test_second_followup_preserves_message_count_and_record(self):
        first = await self.graph.ainvoke({"user_request": "결과는?"}, self.config)
        second = await self.graph.ainvoke({"user_request": "다시 설명해줘"}, self.config)
        self.assertEqual(len(second["messages"]), len(first["messages"]) + 2)
        self.assertEqual(second["analysis_records"], first["analysis_records"])
        context = self.router.calls[-1]["session_context"]
        self.assertTrue(context["recent_turns"])


class FullAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def test_hitl_execution_report_then_followup(self):
        with override_sleep_seconds(0):
            cfg = settings(executor_submit_enabled=True, executor_source_type="INLINE")
            route = FixedAgent({"route": "analysis", "reason": "new analysis"})
            answer = FixedAgent({"answer": "NCE를 사용했습니다."})
            deps = replace(create_mock_dependencies(), routing_agent=route,
                           report_agent=FixedAgent({"content": "# 결과\n100개 행.\n## 한계\n소규모 표본."}),
                           analysis_question_agent=answer)
            execution_id = str(uuid4())
            submit = Mock(return_value={"status_code": 202, "body": {
                "execution_id": execution_id, "operation": {"operation_id": "operation", "steps": []},
                "state": {"status": "QUEUED", "version": 1}}})
            def read_results(_settings, state):
                return [{"tool_id": (step["lineage"].get("input_parameters") or {})["tool_id"],
                         "result": {"status": "SUCCEEDED", "cell_index": step["sequence"],
                                    "outputs": [{"text": "100 rows"}]}}
                        for step in state["execution_steps"]]
            graph = build_analysis_workflow_graph(deps, cfg, checkpointer=InMemorySaver(),
                submit_execution_start=submit, bindings=Mock(register=AsyncMock()), execution_result_reader=read_results)
            config = {"configurable": {"thread_id": str(uuid4())}}
            result = await graph.ainvoke({"user_request": "NCE 품질 분석"}, config)
            result = await graph.ainvoke(Command(resume={"intent": "select", "candidate_id": result["workflow_candidates"][0]["id"]}), config)
            # Reuse production input-display contracts and the existing fixture values.
            from app.graphs.nodes.workflow_inputs_v2 import input_display
            display, _ = input_display(result)
            values = {"data_type": "nce", "lot_cd": "6E2", "process": ["ALL"],
                      "query_mode": "period", "start_dt": "2026-05-01", "end_dt": "2026-05-10", "wafer_list": None}
            inputs = []
            for tool in display.tool_inputs:
                for param in tool.parameters:
                    if param.required and param.binding is None:
                        value = values.get(param.name, param.value if param.has_value else param.default_value)
                        inputs.append({"step_index": tool.step_index, "tool_index": tool.tool_index,
                                       "parameter": param.name, "value": value})
            result = await graph.ainvoke(Command(resume={"intent": "submit_inputs", "workflow_id": result["workflow_id"],
                "workflow_revision": result["workflow_revision"], "inputs": inputs}), config)
            self.assertTrue(result["tool_inputs_confirmed"])
            task_id = result["task_id"]
            self.assertTrue(result["analysis_records"][task_id]["confirmed_inputs"])
            result = await graph.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
            self.assertEqual(result["__interrupt__"][0].value["kind"], "EXECUTOR_EVENT")
            action = {"task_id": task_id, "execution_id": execution_id, "command_id": str(uuid4()),
                "event": {"event_id": str(uuid4()), "execution_id": execution_id,
                          "event_type": "execution.completed", "event_sequence": 1, "schema_version": "1.0",
                          "occurred_at": "2026-10-19T00:00:00Z", "payload": {"status": "SUCCEEDED", "state_version": 2}}}
            with patch("app.graphs.nodes.generate_report.call_async", AsyncMock(return_value={"status_code": 201, "body": {"id": "report-1"}})):
                result = await graph.ainvoke(Command(resume=action), config)
            self.assertEqual(result["report_status"], "generated")
            record = result["analysis_records"][task_id]
            self.assertEqual(record["references"]["report_message_id"], f"report-{task_id}")
            self.assertTrue(record["tool_results"])
            self.assertFalse((await graph.aget_state(config)).next)
            route.result = {"route": "analysis_question", "reason": "follow-up", "analysis_task_ids": [task_id]}
            result = await graph.ainvoke({"user_request": "어떤 데이터를 썼지?"}, config)
            self.assertEqual(result["final_response"]["answer"], "NCE를 사용했습니다.")
            remembered = answer.calls[0]["session_context"]["analyses"][0]
            self.assertTrue(any(item["parameter"] == "data_type" and item["value"] == "nce"
                                for item in remembered["confirmed_inputs"]))


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_invoke_persists_only_new_messages_and_does_not_link_old_analysis(self):
        before = {"session_id": str(uuid4()), "task_id": str(uuid4()), "messages": [{"role": "assistant", "content": "old"}],
                  "notebook": {"old": True}, "final_response": {"status": "report_generated"}}
        after = {**before, "messages": [*before["messages"], {"role": "assistant", "content": "new"}],
                 "final_response": {"status": "analysis_question_answered"}}
        graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values=before)), ainvoke=AsyncMock(return_value=after))
        persistence = SimpleNamespace(persist_state_delta=AsyncMock())
        with patch("app.services.graph_crud_persistence._link_graph_task", AsyncMock()) as link:
            await ainvoke_with_crud_message_persistence(graph, {"user_request": "followup"},
                db=object(), user_id=uuid4(), config={}, dispatcher=persistence, agent_run_id=uuid4())
        link.assert_not_awaited()
        args = persistence.persist_state_delta.call_args
        projected, cursor = args.args[1:3]
        self.assertEqual(cursor.message_count, 1)
        self.assertNotIn("notebook", projected)
        self.assertEqual(projected["final_response"]["status"], "analysis_question_answered")
        events, _ = extract_graph_events(projected, cursor, context=args.kwargs["context"])
        self.assertEqual(len([event for event in events if event.target == "messages"]), 1)

    async def test_stream_skips_restored_artifacts_and_emits_new_identical_answer(self):
        before = {"session_id": str(uuid4()), "task_id": str(uuid4()),
                  "last_received_message_id": "u1",
                  "messages": [{"role": "assistant", "content": "same answer"}],
                  "notebook": {"old": True}, "executor_submit_response": {"old": True},
                  "final_response": {"answer": "same answer"}}
        received = {**before, "last_received_message_id": "u2", "final_response": {},
                    "messages": [*before["messages"], {"role": "user", "content": "again"}]}
        answered = {**received, "final_response": before["final_response"],
                    "messages": [*received["messages"], {"role": "assistant", "content": "same answer"}]}
        async def stream(*args, **kwargs):
            for state in (before, received, answered):
                yield state
        graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values=before)), astream=stream)
        events = []
        async def persist(db, state, cursor, *, context):
            delta, cursor = extract_graph_events(state, cursor, context=context)
            events.extend(delta)
            return SimpleNamespace(cursor=cursor)
        persistence = SimpleNamespace(persist_state_delta=persist)
        with patch("app.services.graph_crud_persistence._link_graph_task", AsyncMock()) as link:
            states = [state async for state in astream_with_crud_message_persistence(
                graph, {"user_request": "again"}, db=object(), user_id=uuid4(), config={},
                dispatcher=persistence, agent_run_id=uuid4())]
        self.assertEqual(len(states), 3)
        link.assert_not_awaited()
        self.assertEqual(len([e for e in events if e.target == "messages"]), 2)
        self.assertFalse([e for e in events if e.target in {"plans", "workflow_logs"}])
        self.assertEqual(len([e for e in events if e.event == "completed"]), 1)
