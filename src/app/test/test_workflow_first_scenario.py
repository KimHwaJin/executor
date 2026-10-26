"""Offline LangGraph interrupts for the Workflow 1.0 scenario."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent_config import load_agent_settings
from app.agents.orchestration.mock_llm import create_mock_dependencies, override_sleep_seconds
from app.graphs.builders.build_analysis_workflow_graph import build_analysis_workflow_graph
from app.services.workflow_persistence import NullWorkflowStore, PostgresWorkflowStore
from app.test.test_workflow_standard_compiler import example, bindings
from app.services.rule_based_notebook_generator import APP_SOURCE_ROOT, build_notebook


class RecordingStore(NullWorkflowStore):
    def __init__(self):
        self.saved = []

    def start_execution(self, **kwargs):
        self.saved.append(deepcopy(kwargs))


class WorkflowFirstTests(unittest.TestCase):
    def test_fixed_load_client_reaches_executor_without_llm(self):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parents[2] / 'tests/load/run_workflow_load.py'
        spec = importlib.util.spec_from_file_location('fixed_load_client', path)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        self.submit.side_effect = None
        self.submit.return_value = {'status_code': 202, 'body': {
            'execution_id': '00000000-0000-0000-0000-000000000042',
            'operation': {'operation_id': 'mock-operation', 'steps': []},
            'state': {'status': 'QUEUED', 'version': 1},
        }}
        self.graph = build_analysis_workflow_graph(self.deps, self.settings,
            workflow_store=self.store, checkpointer=InMemorySaver(),
            submit_execution_start=self.submit, bindings=Mock(register=AsyncMock()))
        with patch('app.agents.orchestration.dependencies.create_chat_model',
                   side_effect=AssertionError('Real LLM is forbidden in mock scenario')):
            result = self.start()
            for expected in ('workflow_candidate_selection', 'additional_information', 'workflow_approval'):
                interaction = result['__interrupt__'][0].value
                self.assertEqual(self.action(result), expected)
                action, payload = client.decision(interaction, client.TOOL_VALUES)
                decision = {'type': action}
                if payload is not None:
                    decision['edited_action'] = {'name': expected, 'args': payload}
                result = self.invoke(Command(resume={'decisions': [decision]}))
            self.submit.assert_called_once()
            self.assertEqual(result['execution_id'], '00000000-0000-0000-0000-000000000042')
            self.assertEqual(result['__interrupt__'][0].value['kind'], 'EXECUTOR_EVENT')

    def setUp(self):
        self.sleep = override_sleep_seconds(0)
        self.sleep.__enter__()
        self.addCleanup(self.sleep.__exit__, None, None, None)
        self.store = RecordingStore()
        self.deps = create_mock_dependencies()
        self.deps.workflow_recommender.invoke = Mock(side_effect=AssertionError("recommendation must be skipped"))
        self.settings = replace(load_agent_settings({}), demo_artifacts_enabled=False, workflow_generation_limit=1)
        self.submit = Mock(side_effect=RuntimeError("execution boundary reached"))
        self.settings = replace(self.settings, executor_submit_enabled=True, executor_source_type="INLINE")
        self.config = {"configurable": {"thread_id": str(uuid4())}}
        self.graph = self.make_graph()

    def make_graph(self):
        return build_analysis_workflow_graph(self.deps, self.settings,
                                            workflow_store=self.store, checkpointer=InMemorySaver(),
                                            submit_execution_start=self.submit)

    def invoke(self, value):
        return asyncio.run(self.graph.ainvoke(value, self.config))

    def action(self, result):
        return result["__interrupt__"][0].value["action_requests"][0]["name"]

    def start(self):
        result = self.invoke({"user_request": "NCE 데이터 품질을 확인해줘"})
        self.assertEqual(self.action(result), "workflow_candidate_selection")
        self.assertNotIn("data_selection", result)
        candidate = result["workflow_candidates"][0]
        self.assertEqual(candidate["workflow"]["workflow_version"], "1.0")
        self.assertNotIn("inputs", candidate["workflow"]["workflow"])
        self.assertEqual(self.store.saved, [])
        self.deps.workflow_recommender.invoke.assert_not_called()
        return result

    def select(self):
        candidate = self.graph.get_state(self.config).values["workflow_candidates"][0]
        return self.invoke(Command(resume={"intent": "select", "candidate_id": candidate["id"]}))

    def answers(self):
        return {"step_0_tool_0_" + k: v for k, v in {
            "data_type": "nce", "lot_cd": "LOT1", "process": ["P01"],
            "start_dt": "2026-01-01", "end_dt": "2026-02-01",
        }.items()}

    def submission(self, answers):
        from app.graphs.nodes.workflow_inputs_v2 import input_display
        state = self.graph.get_state(self.config).values
        display, _ = input_display(state)
        # A UI resubmits displayed required values as well as user edits.
        prefilled = {f"step_{t.step_index}_tool_{t.tool_index}_{p.name}": p.value
                     for t in display.tool_inputs for p in t.parameters
                     if p.required and p.editable and p.has_value}
        answers = {**prefilled, **answers}
        values = []
        for name, value in answers.items():
            _, si, _, ti, parameter = name.split("_", 4)
            values.append({"step_index": int(si), "tool_index": int(ti), "parameter": parameter, "value": value})
        return {"intent": "submit_inputs", "workflow_id": state["workflow_id"],
                "workflow_revision": state["workflow_revision"], "inputs": values}

    @staticmethod
    def parameter(display, si, ti, name):
        return next(p for t in display["tool_inputs"] if (t["step_index"], t["tool_index"]) == (si, ti)
                    for p in t["parameters"] if p["name"] == name)

    def test_selection_input_approval_and_existing_notebook(self):
        initial = self.start()
        result = self.select()
        self.assertEqual(self.action(result), "additional_information")
        result = self.invoke(Command(resume=self.submission(self.answers())))
        self.assertEqual(self.action(result), "workflow_approval")
        self.assertEqual(result["workflow_status"], "ready")
        self.assertEqual(result["workflow_definition"], initial["workflow_definition"])
        self.assertEqual(result["workflow"]["schema_version"], "1.3")
        self.assertNotIn("execution_id", result)
        self.assertEqual(len(build_notebook(result["workflow"], project_root=APP_SOURCE_ROOT)["cells"]), 6)
        # Execution boundary must only be reached after approval.
        self.submit.assert_not_called()
        with self.assertRaisesRegex(RuntimeError, "execution boundary reached"):
            self.invoke(Command(resume={"decisions": [{"type": "approve"}]}))
        self.submit.assert_called_once()

    def test_partial_and_invalid_answers_repeat_input_stage(self):
        self.start()
        self.select()
        result = self.invoke(Command(resume=self.submission({**self.answers(), "step_0_tool_0_data_type": 7})))
        self.assertEqual(self.action(result), "additional_information")
        self.assertTrue(result["input_errors"])
        result = self.invoke(Command(resume=self.submission({"step_0_tool_0_data_type": "nce"})))
        self.assertEqual(self.action(result), "additional_information")
        self.assertFalse(result["tool_inputs_confirmed"])
        result = self.invoke(Command(resume=self.submission(self.answers())))
        self.assertEqual(self.action(result), "workflow_approval")

    def test_no_inputs_still_requires_selection_and_final_approval(self):
        document = self.deps.workflow_agent.value.model_dump()
        for name, value in self.answers().items():
            parameter = name.removeprefix("step_0_tool_0_")
            document["workflow"]["steps"][0]["tools"][0]["arguments"][parameter] = {"source": "literal", "value": value}
        document["workflow"]["allow_immediate_execution"] = True
        self.deps.workflow_agent.value = document
        self.start()
        self.assertEqual(self.action(self.select()), "additional_information")
        result = self.invoke(Command(resume=self.submission(self.answers())))
        self.assertEqual(self.action(result), "workflow_approval")

    def test_rejection_generates_new_candidate_and_clears_inputs(self):
        self.start()
        self.select()
        self.invoke(Command(resume=self.submission(self.answers())))
        result = self.invoke(Command(resume={"intent": "reject", "reason": "통계를 수정해줘"}))
        self.assertEqual(self.action(result), "workflow_candidate_selection")
        self.assertEqual(result["execution_inputs"], [])
        self.assertEqual(len(result["workflow_candidates"]), 1)
        self.assertEqual(result["workflow_revision"], 2)

    def test_adaptive_definition_through_input_and_notebook(self):
        document = example("adaptive")
        # The current eda_analysis skill requires histogram_eda in every plan.
        eda = next(step for step in document["workflow"]["steps"] if step["skill"] == "eda_analysis")
        if not any(tool["tool"] == "histogram_eda" for tool in eda["tools"]):
            eda["tools"].append({"tool": "histogram_eda", "execution": "always",
                                 "arguments": {"data": deepcopy(eda["tools"][0]["arguments"]["data"])}})
        self.deps.workflow_agent.value = document
        self.deps.skill_selector_agent.value = {"skill_names": [s["skill"] for s in document["workflow"]["steps"]]}
        self.start()
        self.select()
        answers = {f"step_{b['step_index']}_tool_{b['tool_index']}_{b['parameter']}": b["value"] for b in bindings(document)}
        result = self.invoke(Command(resume=self.submission(answers)))
        self.assertEqual(self.action(result), "workflow_approval")
        self.assertEqual(result["workflow"]["workflow"]["execution_mode"], "adaptive")
        self.assertTrue(build_notebook(result["workflow"], project_root=APP_SOURCE_ROOT)["cells"])

    def test_agent_chat_selection_transport(self):
        self.start()
        candidate = self.graph.get_state(self.config).values["workflow_candidates"][0]
        result = self.invoke(Command(resume={"decisions": [{"type": "edit", "edited_action": {
            "name": "workflow_candidate_selection", "args": {"intent": "select", "candidate_id": candidate["id"]},
        }}]}))
        self.assertEqual(self.action(result), "additional_information")
        result = self.invoke(Command(resume={"decisions": [{"type": "edit", "edited_action": {
            "name": "additional_information", "args": self.submission(self.answers()),
        }}]}))
        self.assertEqual(self.action(result), "workflow_approval")

    def test_generation_retries_schema_error_without_asking_for_data(self):
        valid = self.deps.workflow_agent.value
        agent = Mock(ainvoke=AsyncMock())
        payloads = []
        def generate(payload):
            payloads.append(deepcopy(payload))
            return {"workflow_version": "invalid"} if len(payloads) == 1 else valid
        agent.ainvoke = AsyncMock(side_effect=generate)
        self.deps = replace(self.deps, workflow_agent=agent)
        self.graph = self.make_graph()
        self.start()
        self.assertEqual(len(payloads), 2)
        self.assertIn("validation_feedback", payloads[1])
        self.assertNotIn("data_selection", payloads[0])

    def test_request_values_survive_selection_and_prefill_the_input_form(self):
        document = self.deps.workflow_agent.value.model_dump(mode="json")
        arguments = document["workflow"]["steps"][0]["tools"][0]["arguments"]
        arguments["limit"] = {"source": "input"}
        document["execution_inputs"] = [
            {"step_index": 0, "tool_index": 0, "parameter": parameter,
             "value": value, "origin": "user_answer", "confirmed": True}
            for parameter, value in {"data_type": "nce", "lot_cd": "LOT1", "limit": 0}.items()
        ]
        self.deps.workflow_agent.value = document
        initial = self.start()
        self.assertTrue(all(v["origin"] == "user_request" and not v["confirmed"]
                            for v in initial["execution_inputs"]))
        self.assertNotIn("execution_inputs", initial["workflow_candidates"][0]["workflow"])
        self.assertEqual(initial["workflow_definition"]["workflow"]["steps"][0]["tools"][0]
                         ["arguments"]["lot_cd"], {"source": "input"})
        result = self.select()
        args = result["__interrupt__"][0].value["action_requests"][0]["args"]
        self.assertEqual(self.parameter(args, 0, 0, "lot_cd")["value"], "LOT1")
        self.assertEqual(self.parameter(args, 0, 0, "data_type")["value"], "nce")
        self.assertEqual(self.parameter(args, 0, 0, "limit")["value"], 0)
        self.assertFalse(self.parameter(args, 0, 0, "start_dt")["has_value"])
        result = self.invoke(Command(resume=self.submission({**self.answers(), "step_0_tool_0_lot_cd": "LOT2",
                                            "step_0_tool_0_limit": 0})))
        self.assertEqual(self.action(result), "workflow_approval")
        self.assertEqual(result["workflow"]["workflow"]["inputs"]["step_0_tool_0_lot_cd"], "LOT2")
        self.assertTrue(all(v["confirmed"] for v in result["execution_inputs"]))

    def test_invalid_prefill_call_address_is_retried(self):
        valid = self.deps.workflow_agent.value.model_dump(mode="json")
        invalid = deepcopy(valid)
        invalid["execution_inputs"] = [{"step_index": 99, "tool_index": 0,
                                        "parameter": "lot_cd", "value": "LOT1"}]
        agent = Mock(ainvoke=AsyncMock())
        agent.ainvoke = AsyncMock(side_effect=[invalid, valid])
        self.deps = replace(self.deps, workflow_agent=agent)
        self.graph = self.make_graph()
        self.start()
        self.assertEqual(agent.ainvoke.call_count, 2)

    def test_missing_required_argument_becomes_an_input_without_retry(self):
        document = self.deps.workflow_agent.value.model_dump(mode="json")
        del document["workflow"]["steps"][0]["tools"][0]["arguments"]["lot_cd"]
        agent = Mock(ainvoke=AsyncMock())
        agent.ainvoke.return_value = document
        self.deps = replace(self.deps, workflow_agent=agent)
        self.graph = self.make_graph()
        result = self.start()
        self.assertEqual(result["workflow_definition"]["workflow"]["steps"][0]["tools"][0]
                         ["arguments"]["lot_cd"], {"source": "input"})
        agent.ainvoke.assert_called_once()
        result = self.select()
        args = result["__interrupt__"][0].value["action_requests"][0]["args"]
        self.assertFalse(self.parameter(args, 0, 0, "lot_cd")["has_value"])

    def test_optional_input_displays_registry_default_and_can_be_omitted_or_edited(self):
        document = self.deps.workflow_agent.value.model_dump(mode="json")
        document["workflow"]["steps"][0]["tools"][1]["arguments"]["transform_op"] = {"source": "input"}
        data = {"source": "tool_output", "step_index": 0, "tool_index": 1, "output": "data"}
        document["workflow"]["steps"].append({"skill": "eda_analysis", "tools": [
            {"tool": "compute_statistics", "execution": "always", "arguments": {"data": data}},
            {"tool": "histogram_eda", "execution": "always", "arguments": {
                "data": data, "bins": {"source": "input"}}},
        ]})
        self.deps.workflow_agent.value = document
        self.deps.skill_selector_agent.value = {"skill_names": ["data_load", "data_quality_check", "eda_analysis"]}
        for answer, expected in (({}, 30), ({"step_2_tool_1_bins": 12}, 12)):
            with self.subTest(answer=answer):
                self.config = {"configurable": {"thread_id": str(uuid4())}}
                self.graph = self.make_graph()
                self.start()
                result = self.select()
                request = result["__interrupt__"][0].value
                args = request["action_requests"][0]["args"]
                schema = request["review_configs"][0]["args_schema"]
                self.assertEqual(self.parameter(args, 2, 1, "bins")["value"], 30)
                self.assertIsNone(self.parameter(args, 0, 1, "transform_op")["value"])
                self.assertFalse(self.parameter(args, 2, 1, "bins")["required"])
                self.assertTrue(self.parameter(args, 0, 0, "lot_cd")["required"])
                self.assertEqual(self.parameter(args, 2, 1, "bins")["default_value"], 30)
                result = self.invoke(Command(resume=self.submission({**self.answers(), **answer})))
                self.assertEqual(self.action(result), "workflow_approval")
                self.assertEqual(result["workflow"]["workflow"]["inputs"]["step_2_tool_1_bins"], expected)

    def test_generator_prompt_includes_excel_query_matrix_separately_from_skills(self):
        from app.agents.workflow_generator.create_workflow_generator_agent import create_workflow_generator_agent
        from app.agents.workflow_generator.workflow_generator_tools import read_skill_documents
        resources = read_skill_documents.invoke({"skill_names": ["data_load"]})
        self.assertNotIn("data_selection.xlsx", resources["skills"]["data_load"]["content"])
        with patch("app.agents.workflow_generator.create_workflow_generator_agent.create_agent") as factory:
            create_workflow_generator_agent(object())
        content = factory.call_args.kwargs["system_prompt"]
        self.assertIn("# data_selection", content)
        self.assertIn("| NCE | period | O | O | O | O |", content)
        self.assertIn("| WT_SYMBOL | wafer_list | O | O |", content)
        self.assertIn("execution_inputs", content)

    def test_generation_rejects_execution_mismatch_without_rewriting(self):
        document = example()
        document["workflow"]["steps"][2]["tools"][1]["execution"] = "conditional"
        document["workflow"]["execution_mode"] = "adaptive"
        original = deepcopy(document)
        agent = Mock(ainvoke=AsyncMock())
        agent.ainvoke.return_value = document
        self.deps = replace(self.deps, workflow_agent=agent)
        self.deps.skill_selector_agent.value = {
            "skill_names": [s["skill"] for s in document["workflow"]["steps"]]
        }
        self.graph = self.make_graph()
        with self.assertRaisesRegex(RuntimeError, "execution must be always"):
            self.start()
        self.assertEqual(agent.ainvoke.call_count, 3)
        self.assertIn("execution must be always", agent.ainvoke.call_args.args[0]["validation_feedback"])
        self.assertEqual(document, original)

    def test_candidate_rejection_regenerates(self):
        self.start()
        result = self.invoke(Command(resume={"intent": "reject", "reason": "다른 Workflow를 만들어줘"}))
        self.assertEqual(self.action(result), "workflow_candidate_selection")
        self.assertEqual(result["workflow_revision"], 2)
        self.submit.assert_not_called()

    def test_v2_regeneration_excludes_old_candidate_and_preserves_request(self):
        first = self.start()
        request = first["__interrupt__"][0].value
        self.assertEqual(request["payload_version"], 2)
        self.assertEqual([a["intent"] for a in request["action_requests"][0]["args"]["available_actions"]],
                         ["select", "regenerate", "reject"])
        original = self.deps.workflow_agent.value.model_dump()
        alternative = deepcopy(original)
        tools = alternative["workflow"]["steps"][-1]["tools"]
        tools.append(deepcopy(tools[-1]))
        self.deps.workflow_agent.value = alternative
        result = self.invoke(Command(resume={"intent": "regenerate"}))
        self.assertEqual(self.action(result), "workflow_candidate_selection")
        self.assertEqual(result["user_request"], first["user_request"])
        self.assertEqual(result["workflow_revision"], 2)
        self.assertEqual(len(result["workflow_candidates"]), 1)
        self.assertNotEqual(result["workflow_candidates"][0]["id"], first["workflow_candidates"][0]["id"])
        self.assertEqual(len(result["excluded_workflow_candidates"]), 1)
        self.assertNotEqual(result["workflow_definition"], first["workflow_definition"])
        self.deps.workflow_recommender.invoke.assert_not_called()
        self.submit.assert_not_called()

    def test_recommended_generate_enters_generation_path(self):
        first = self.start()
        candidates = deepcopy(first["workflow_candidates"])
        candidates[0].update(id="recommended_1", origin="recommended")
        # Inject recommendation output without implementing the search backend.
        self.graph.update_state(self.config, {"workflow_candidates": candidates},
                                as_node="add_generated_workflow_candidate")
        pending = self.invoke(None)
        actions = pending["__interrupt__"][0].value["action_requests"][0]["args"]["available_actions"]
        self.assertIn("generate", [a["intent"] for a in actions])
        result = self.invoke(Command(resume={"intent": "generate"}))
        self.assertEqual(self.action(result), "workflow_candidate_selection")
        self.assertEqual(result["workflow_origin"], "generated")
        self.assertEqual(result["workflow_revision"], 2)
        self.assertEqual(result["user_request"], first["user_request"])
        self.assertEqual(len(result["workflow_candidates"]), 1)
        self.assertEqual(result["workflow_candidates"][0]["origin"], "generated")
        self.assertFalse(result.get("excluded_workflow_candidates"))
        self.submit.assert_not_called()

    def test_v2_regeneration_never_reoffers_identical_structure(self):
        self.start()
        with self.assertRaisesRegex(RuntimeError, "excluded candidate"):
            self.invoke(Command(resume={"intent": "regenerate"}))
        self.submit.assert_not_called()

    def test_v2_rejection_stop_is_decided_by_router(self):
        router = Mock(ainvoke=AsyncMock())
        router.ainvoke.side_effect = lambda p: {
            "route": "cancel" if p.get("routing_context") == "workflow_rejected" else "analysis",
            "reason": "stop requested",
        }
        self.deps = replace(self.deps, routing_agent=router)
        self.graph = self.make_graph()
        self.start()
        reason = "분석 자체를 하지 않겠습니다."
        result = self.invoke(Command(resume={"intent": "reject", "reason": reason}))
        self.assertEqual(router.ainvoke.call_args.args[0]["approval_feedback"], reason)
        self.assertEqual(result["final_response"]["status"], "cancelled")
        self.submit.assert_not_called()

    def test_revise_data_route_retains_previous_definition_for_generation(self):
        router = Mock(ainvoke=AsyncMock())
        router.ainvoke.side_effect = lambda p: {
            "route": "revise_workflow" if p.get("routing_context") == "workflow_rejected" else "analysis",
            "reason": "test routing",
        }
        generator = Mock(ainvoke=AsyncMock())
        generator.ainvoke.return_value = self.deps.workflow_agent.value
        self.deps = replace(self.deps, routing_agent=router, workflow_agent=generator)
        self.graph = self.make_graph()
        first = self.start()
        result = self.invoke(Command(resume={"intent": "reject", "reason": "데이터를 다시 선택할게"}))
        self.assertEqual(self.action(result), "workflow_candidate_selection")
        self.assertEqual(generator.ainvoke.call_args.args[0]["previous_workflow"], first["workflow_definition"])
        self.assertEqual(result["execution_inputs"], [])

    def test_workflow_json_is_not_written_even_with_demo_enabled(self):
        from app.graphs.nodes.demo_artifacts import make_save_approved_workflow
        self.settings = replace(self.settings, demo_artifacts_enabled=True)
        self.graph = self.make_graph()
        with patch("app.graphs.nodes.hitl.write_demo_json") as selected_write:
            self.start()
            self.select()
            selected_write.assert_not_called()
        result = self.invoke(Command(resume=self.submission(self.answers())))
        result["approval"] = {"approved": True}
        with patch("app.graphs.nodes.demo_artifacts.write_demo_json") as approved_write:
            self.assertEqual(make_save_approved_workflow(self.settings)(result), {})
            approved_write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
