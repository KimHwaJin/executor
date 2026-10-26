"""Real compiler and API resume boundary for the all-parameter input stage."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, END, StateGraph
from langgraph.types import Command

from app.agents.orchestration.mock_llm import create_mock_dependencies
from app.agents.workflow_generator.workflow_standard_compiler import compile_workflow
from app.graphs.nodes.workflow_inputs import collect_standard_inputs
from app.graphs.nodes.workflow_inputs_v2 import input_display, agent_chat_input_form
from app.graphs.nodes.generate_workflow import apply_missing_information_to_workflow
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.schemas.common.workflow_run_schema import WorkflowRunRequest
from app.services.workflow_interactions import public_interactions
from app.services.workflow_run_service import resume_command


class ToolInputV2Tests(unittest.TestCase):
    def state(self):
        document = create_mock_dependencies().workflow_agent.value.model_dump(mode="json")
        document["workflow"]["steps"][0]["tools"][0]["arguments"]["limit"] = {"source": "input"}
        workflow_id = str(uuid4())
        inputs = [{"step_index": 0, "tool_index": 0, "parameter": "limit", "value": 17,
                   "origin": "user_request", "confirmed": False}]
        return {"workflow_definition": document, "workflow_id": workflow_id, "workflow_revision": 1,
                "tool_input_payload_version": 2, "execution_inputs": inputs,
                "workflow": compile_workflow(document, workflow_id=workflow_id, execution_inputs=inputs)}

    def payload(self, state):
        display, _ = input_display(state)
        answers = {"data_type": "nce", "lot_cd": "LOT1", "process": ["P01"],
                   "start_dt": "2026-01-01", "end_dt": "2026-02-01"}
        values = []
        for tool in display.tool_inputs:
            for p in tool.parameters:
                if p.editable and p.required and p.binding is None:
                    values.append({"step_index": tool.step_index, "tool_index": tool.tool_index,
                                   "parameter": p.name, "value": p.value if p.has_value else answers[p.name]})
        return {"intent": "submit_inputs", "workflow_id": state["workflow_id"], "workflow_revision": 1,
                "inputs": values}

    def collect(self, state, payload):
        with patch("app.graphs.nodes.workflow_inputs_v2.request_human_input", return_value=payload):
            return collect_standard_inputs(state)

    def test_display_includes_defaults_prefill_and_readonly_bindings(self):
        state = self.state()
        original = deepcopy(state)
        display, _ = input_display(state)
        params = {(t.step_index, t.tool_index, p.name): p for t in display.tool_inputs for p in t.parameters}
        self.assertFalse(params[0, 0, "lot_cd"].has_value)
        self.assertEqual(params[0, 0, "limit"].value, 17)
        self.assertEqual(params[0, 0, "limit"].value_origin, "user_request")
        self.assertEqual(params[0, 0, "query_mode"].value_origin, "workflow_literal")
        self.assertTrue(params[0, 0, "query_mode"].editable)
        self.assertEqual(params[1, 2, "method"].value, "iqr")
        self.assertTrue(params[0, 1, "data"].editable)
        self.assertEqual(params[0, 1, "data"].binding["output"], "data")
        self.assertEqual(state, original)

    def filled_agent_chat_form(self, state):
        display, _ = input_display(state)
        args, _ = agent_chat_input_form(display)
        for item in self.payload(state)["inputs"]:
            args[f"step_{item['step_index']}_tool_{item['tool_index']}_{item['parameter']}"] = json.dumps(item["value"])
        return args

    def test_dev_agent_chat_flat_fields_resume_through_checkpoint(self):
        state = self.state()
        from app.graphs.nodes.hitl import collect_missing_information
        builder = StateGraph(AnalysisWorkflowState)
        builder.add_node("inputs", collect_missing_information)
        builder.add_edge(START, "inputs")
        builder.add_edge("inputs", END)
        graph = builder.compile(checkpointer=InMemorySaver()).with_config(
            configurable={"agent_chat_input_form": True})
        config = {"configurable": {"thread_id": str(uuid4())}}
        request = graph.invoke(state, config)["__interrupt__"][0].value
        args = request["action_requests"][0]["args"]
        self.assertNotIn("tool_inputs", args)
        self.assertEqual(args["step_0_tool_0_lot_cd"], "")
        self.assertEqual(args["step_0_tool_0_limit"], "17")
        self.assertTrue(args["step_0_tool_1_data"].startswith("@output "))
        filled = self.filled_agent_chat_form(state)
        filled["step_0_tool_0_lot_cd"] = "00123"
        filled["step_0_tool_0_limit"] = ""
        filled["step_0_tool_1_transform_op"] = "null"
        result = graph.invoke(Command(resume={"decisions": [{"type": "edit", "edited_action": {
            "name": "additional_information", "args": filled}}]}), config)
        self.assertTrue(result["tool_inputs_confirmed"])
        bindings = {(v["step_index"], v["tool_index"], v["parameter"]): v["value"]
                    for v in result["execution_inputs"]}
        self.assertEqual(bindings[0, 0, "lot_cd"], "00123")
        self.assertIsNone(bindings[0, 0, "limit"])
        self.assertIsNone(bindings[0, 1, "transform_op"])

    def test_dev_agent_chat_fields_do_not_bypass_validation(self):
        state = self.state()
        valid = self.filled_agent_chat_form(state)
        for updates in [{"workflow_revision": 2}, {"workflow_id": "wrong"},
                        {"step_0_tool_0_lot_cd": ""}, {"step_0_tool_0_limit": "wrong"},
                        {"step_0_tool_1_data": "@output {invalid}"}, {"step_99_tool_0_unknown": "1"}]:
            with self.subTest(updates=updates):
                with patch("app.graphs.nodes.workflow_inputs_v2.request_human_input", return_value={**valid, **updates}):
                    result = collect_standard_inputs(state, {"configurable": {"agent_chat_input_form": True}})
                self.assertFalse(result["tool_inputs_confirmed"])
                self.assertNotIn("execution_inputs", result)
                self.assertTrue(result["input_errors"])

    def test_dev_entrypoint_still_accepts_canonical_v2_submission(self):
        state = self.state()
        with patch("app.graphs.nodes.workflow_inputs_v2.request_human_input", return_value=self.payload(state)):
            result = collect_standard_inputs(state, {"configurable": {"agent_chat_input_form": True}})
        self.assertTrue(result["tool_inputs_confirmed"])

    def test_pending_old_dev_display_reissues_flat_form_after_restart(self):
        state = self.state()
        builder = StateGraph(AnalysisWorkflowState)
        builder.add_node("inputs", collect_standard_inputs)
        builder.add_edge(START, "inputs")
        builder.add_conditional_edges("inputs", lambda s: END if s.get("tool_inputs_confirmed") else "inputs")
        saver = InMemorySaver()
        graph = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": str(uuid4())}}
        old_request = graph.invoke(state, config)["__interrupt__"][0].value
        old_args = old_request["action_requests"][0]["args"]
        self.assertIn("tool_inputs", old_args)
        graph = builder.compile(checkpointer=saver).with_config(configurable={"agent_chat_input_form": True})
        result = graph.invoke(Command(resume={"decisions": [{"type": "edit", "edited_action": {
            "name": "additional_information", "args": old_args}}]}), config)
        new_request = result["__interrupt__"][0].value
        self.assertIn("step_0_tool_0_lot_cd", new_request["action_requests"][0]["args"])
        self.assertNotIn("tool_inputs", new_request["action_requests"][0]["args"])
        self.assertFalse(result["tool_inputs_confirmed"])
        self.assertNotIn("workflow_input_definition", result)

    def test_omission_restores_default_not_prefill_and_keeps_original(self):
        state = self.state()
        original = deepcopy(state["workflow_definition"])
        result = self.collect(state, self.payload(state))
        self.assertTrue(result["tool_inputs_confirmed"])
        display, _ = input_display(state)
        expected = display.tool_inputs[0].parameters
        limit_default = next(p.default_value for p in expected if p.name == "limit")
        limit = next(i for i in result["execution_inputs"] if i["parameter"] == "limit")
        self.assertEqual(limit["value"], limit_default)
        compiled = apply_missing_information_to_workflow({**state, **result})
        self.assertEqual(compiled["workflow_status"], "ready")
        self.assertEqual(compiled["messages"][0]["role"], "agent")
        self.assertEqual(state["workflow_definition"], original)

    def test_repeated_tools_have_independent_addresses(self):
        state = self.state()
        tools = state["workflow_definition"]["workflow"]["steps"][1]["tools"]
        tools.append(deepcopy(tools[-1]))
        payload = self.payload(state)
        payload["inputs"] += [{"step_index": 1, "tool_index": ti, "parameter": "method", "value": method}
                              for ti, method in [(2, "iqr"), (3, "zscore")]]
        result = self.collect(state, payload)
        self.assertTrue(result["tool_inputs_confirmed"])
        compiled = apply_missing_information_to_workflow({**state, **result})["workflow"]["workflow"]
        self.assertEqual(compiled["inputs"]["step_1_tool_2_method"], "iqr")
        self.assertEqual(compiled["inputs"]["step_1_tool_3_method"], "zscore")

    def test_invalid_submissions_never_replace_bindings(self):
        state = self.state()
        valid = self.payload(state)
        for mutate in [
            lambda p: p.update(workflow_revision=2),
            lambda p: p.update(workflow_id=str(uuid4())),
            lambda p: p["inputs"].append(deepcopy(p["inputs"][0])),
            lambda p: p["inputs"].pop(0),
            lambda p: p["inputs"][0].update(value=99),
            lambda p: p["inputs"].append({"step_index": 0, "tool_index": 1, "parameter": "data", "source": "tool_output",
                                       "reference": {"step_index": 9, "tool_index": 0, "output": "data"}}),
            lambda p: p["inputs"].append({"step_index": 99, "tool_index": 0, "parameter": "x", "value": 1}),
            lambda p: p["inputs"].append({"step_index": 1, "tool_index": 2, "parameter": "method", "value": None}),
        ]:
            payload = deepcopy(valid)
            mutate(payload)
            with self.subTest(payload=payload):
                result = self.collect(state, payload)
                self.assertFalse(result["tool_inputs_confirmed"])
                self.assertTrue(result["input_errors"])
                self.assertNotIn("execution_inputs", result)

    def test_explicit_null_is_preserved_when_nullable(self):
        state = self.state()
        payload = self.payload(state)
        payload["inputs"].append({"step_index": 0, "tool_index": 1, "parameter": "transform_op", "value": None})
        result = self.collect(state, payload)
        self.assertTrue(result["tool_inputs_confirmed"])
        self.assertIsNone(next(v["value"] for v in result["execution_inputs"] if v["parameter"] == "transform_op"))

    def test_actual_interrupt_public_projection_api_resume_and_checkpoint(self):
        state = self.state()
        builder = StateGraph(AnalysisWorkflowState)
        builder.add_node("inputs", collect_standard_inputs)
        builder.add_edge(START, "inputs")
        builder.add_edge("inputs", END)
        saver = InMemorySaver()
        graph = builder.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": str(uuid4())}}
        request = graph.invoke(state, config)["__interrupt__"][0].value
        self.assertEqual(request["payload_version"], 2)
        run = SimpleNamespace(run_id=uuid4(), task_id=uuid4(), interrupt=[request])
        interaction = public_interactions(run)[0]
        self.assertEqual(interaction["payload"]["arguments"], request["action_requests"][0]["args"])
        payload = self.payload(state)
        payload["inputs"].append({"step_index": 1, "tool_index": 0, "parameter": "data",
                                  "source": "tool_output", "reference": {
                                      "step_index": 0, "tool_index": 0, "output": "data"}})
        resume = WorkflowRunRequest.model_validate({
            "run_id": str(run.run_id), "task_id": str(run.task_id),
            "command": {"resume": {"interaction_id": interaction["interaction_id"], "revision": 1,
                                     "action": "edit", "payload": payload}},
        }).command.resume
        stale = resume.model_copy(deep=True)
        stale.payload["workflow_revision"] = 2
        with self.assertRaises(HTTPException) as caught:
            resume_command(run, stale)
        self.assertEqual(caught.exception.status_code, 409)
        # Simulate reloading the graph with an existing pending checkpoint.
        graph = builder.compile(checkpointer=saver)
        result = graph.invoke(Command(resume=resume_command(run, resume)), config)
        self.assertTrue(result["tool_inputs_confirmed"])
        binding = result["workflow_input_definition"]["workflow"]["steps"][1]["tools"][0]["arguments"]["data"]
        self.assertEqual(binding, {"source": "tool_output", "step_index": 0, "tool_index": 0, "output": "data"})

    def test_pending_v1_without_version_keeps_legacy_submission(self):
        state = self.state()
        del state["tool_input_payload_version"]
        answers = {"step_0_tool_0_" + k: v for k, v in {
            "data_type": "nce", "lot_cd": "LOT1", "process": ["P01"],
            "start_dt": "2026-01-01", "end_dt": "2026-02-01",
        }.items()}
        with patch("app.graphs.nodes.workflow_inputs.request_human_input", return_value=answers) as request:
            result = collect_standard_inputs(state)
        self.assertNotIn("payload_version", request.call_args.kwargs)
        self.assertEqual(result["input_errors"], "")
        self.assertNotIn("tool_inputs_confirmed", result)

    def test_cli_default_reset_omits_value_and_skips_readonly_parameters(self):
        from cli import _prompt_additional_information
        display, _ = input_display(self.state())
        with patch("builtins.input", side_effect=lambda prompt: ":default" if "limit [" in prompt else ""), patch("builtins.print"):
            result = _prompt_additional_information(display.model_dump(mode="json"))
        self.assertEqual(result["intent"], "submit_inputs")
        self.assertTrue(any(i["parameter"] == "limit" and i.get("source") == "default" for i in result["inputs"]))
        self.assertTrue(any(i["parameter"] == "data" and i.get("source") == "tool_output" for i in result["inputs"]))
        self.assertTrue(any(i["parameter"] == "query_mode" for i in result["inputs"]))


    def test_catalog_is_shared_and_references_are_validated(self):
        from app.schemas.common.workflow_output_catalog import output_reference_allowed
        display, _ = input_display(self.state())
        data = display.model_dump(mode="json")
        self.assertTrue(data["output_catalog"])
        self.assertTrue(all("output_options" not in p for t in data["tool_inputs"] for p in t["parameters"]))
        ref = {"step_index": 0, "tool_index": 0, "output": "data"}
        self.assertFalse(output_reference_allowed(data, (0, 0, "data"), ref))
        self.assertTrue(output_reference_allowed(data, (0, 1, "data"), ref))
        self.assertFalse(output_reference_allowed(data, (0, 1, "data"), {**ref, "tool_index": 1}))
        catalog = deepcopy(data)
        for output in catalog["output_catalog"][0]["tools"][0]["outputs"]:
            output["selectable"] = False
        self.assertFalse(output_reference_allowed(catalog, (0, 1, "data"), ref))

    def test_output_value_default_transitions_and_original_preservation(self):
        state = self.state()
        original = deepcopy(state)
        payload = self.payload(state)
        target = {"step_index": 1, "tool_index": 0, "parameter": "data"}
        payload["inputs"].append({**target, "source": "tool_output", "reference": {
            "step_index": 0, "tool_index": 0, "output": "data"}})
        result = self.collect(state, payload)
        self.assertTrue(result["tool_inputs_confirmed"], result)
        args = result["workflow_input_definition"]["workflow"]["steps"][1]["tools"][0]["arguments"]
        self.assertEqual(args["data"]["tool_index"], 0)
        compiled = apply_missing_information_to_workflow({**state, **result})["workflow"]
        self.assertIn("step_0_tool_0.outputs.data", compiled["workflow"]["steps"][1]["tools"][0]["arguments"]["data"])
        payload["inputs"][-1] = {**target, "source": "value", "value": {"sample": [1]}}
        result = self.collect(state, payload)
        self.assertTrue(result["tool_inputs_confirmed"], result)
        self.assertEqual(result["workflow_input_definition"]["workflow"]["steps"][1]["tools"][0]["arguments"]["data"], {"source": "input"})
        self.assertEqual(state, original)

    def test_api_accepts_reference_and_rejects_unknown_output(self):
        from app.services.workflow_hitl_validation import validate_v2
        display, _ = input_display(self.state())
        action = {"name": "additional_information", "args": display.model_dump(mode="json")}
        payload = self.payload(self.state())
        payload["workflow_id"] = display.workflow_id
        payload["inputs"].append({"step_index": 1, "tool_index": 0, "parameter": "data",
                                  "source": "tool_output", "reference": {
                                      "step_index": 0, "tool_index": 1, "output": "data"}})
        validate_v2(action, payload)
        payload["inputs"][-1]["reference"]["output"] = "not_registered"
        with self.assertRaises(HTTPException):
            validate_v2(action, payload)

    def test_explicit_default_replaces_existing_optional_output(self):
        state = self.state()
        tool = state["workflow_definition"]["workflow"]["steps"][1]["tools"][1]
        tool["arguments"]["columns"] = {"source": "tool_output", "step_index": 0, "tool_index": 0, "output": "data"}
        payload = self.payload(state)
        payload["inputs"].append({"step_index": 1, "tool_index": 1, "parameter": "columns", "source": "default"})
        result = self.collect(state, payload)
        self.assertTrue(result["tool_inputs_confirmed"], result)
        self.assertIsNone(next(v["value"] for v in result["execution_inputs"] if v["parameter"] == "columns"))

    def test_legacy_output_options_remain_supported(self):
        from app.services.workflow_hitl_validation import validate_v2
        from app.schemas.common.workflow_hitl_schema import ToolInputDisplay, build_tool_input_submission_schema
        from jsonschema import Draft202012Validator
        display, _ = input_display(self.state())
        data = display.model_dump(mode="json")
        data.pop("output_catalog")
        parameter = data["tool_inputs"][1]["parameters"][0]
        parameter["output_options"] = [{"step_index": 0, "tool_index": 0, "output": "data",
                                         "skill": "data_load", "tool": "extract_data"}]
        schema = build_tool_input_submission_schema(ToolInputDisplay.model_validate(data))
        payload = self.payload(self.state())
        payload["workflow_id"] = display.workflow_id
        payload["inputs"].append({"step_index": 0, "tool_index": 1, "parameter": parameter["name"],
                                  "source": "tool_output", "reference": {
                                      "step_index": 0, "tool_index": 0, "output": "data"}})
        Draft202012Validator(schema).validate(payload)
        validate_v2({"name": "additional_information", "args": data}, payload)

    def test_reference_schema_does_not_repeat_catalog_per_parameter(self):
        from app.schemas.common.workflow_hitl_schema import build_tool_input_submission_schema
        display, _ = input_display(self.state())
        first = build_tool_input_submission_schema(display)
        output = display.output_catalog[0].tools[0].outputs[0]
        for index in range(100):
            display.output_catalog[0].tools[0].outputs.append(output.model_copy(update={"name": f"extra_{index}"}))
        self.assertEqual(first, build_tool_input_submission_schema(display))

    def test_api_and_graph_reject_catalog_bypass(self):
        from app.services.workflow_hitl_validation import validate_v2
        state = self.state()
        display, definition = input_display(state)
        payload = self.payload(state)
        item = {"step_index": 1, "tool_index": 0, "parameter": "data", "source": "tool_output",
                "reference": {"step_index": 0, "tool_index": 0, "output": "data"}}
        payload["inputs"].append(item)
        for reference in [
            {"step_index": 1, "tool_index": 0, "output": "result"},
            {"step_index": 1, "tool_index": 1, "output": "result"},
            {"step_index": 0, "tool_index": 0, "output": "invented"},
        ]:
            item["reference"] = reference
            with self.subTest(reference=reference):
                with self.assertRaises(HTTPException):
                    validate_v2({"name": "additional_information", "args": display.model_dump(mode="json")}, payload)
                self.assertFalse(self.collect(state, payload)["tool_inputs_confirmed"])
        item["reference"] = {"step_index": 0, "tool_index": 0, "output": "data"}
        for output in display.output_catalog[0].tools[0].outputs:
            output.selectable = False
        with self.assertRaises(HTTPException):
            validate_v2({"name": "additional_information", "args": display.model_dump(mode="json")}, payload)
        with patch("app.graphs.nodes.workflow_inputs_v2.input_display", return_value=(display, definition)):
            self.assertFalse(self.collect(state, payload)["tool_inputs_confirmed"])
