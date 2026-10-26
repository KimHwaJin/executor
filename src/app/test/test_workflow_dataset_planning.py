"""Dataset planning stays advisory while Tool contracts remain validated."""

from copy import deepcopy
from dataclasses import replace
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from agent_config import load_agent_settings
from app.agents.orchestration.mock_llm import create_mock_dependencies
from app.graphs.nodes.generate_workflow import make_generate_workflow
from app.schemas.agents.workflow_generator_schema import DatasetRequirement
from app.schemas.common.dataset_types import DATASET_TYPES
from app.agents.workflow_generator.workflow_standard_compiler import compile_workflow
from app.graphs.nodes.workflow_inputs_v2 import input_display
from jsonschema import Draft202012Validator


def output(step, tool, name="data"):
    return {"source": "tool_output", "step_index": step, "tool_index": tool, "output": name}


class DatasetPlanningTests(unittest.TestCase):
    def test_dataset_type_enum_normalizes_only_supported_sources(self):
        for dtype in DATASET_TYPES:
            self.assertEqual(DatasetRequirement(data_type=dtype.upper(), role="features").data_type, dtype)
        for invalid in ["PT1H", "fail", "unknown", "", None, 7]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                DatasetRequirement(data_type=invalid, role="target")
        self.assertEqual(DatasetRequirement.model_json_schema()["properties"]["data_type"]["enum"], list(DATASET_TYPES))

    def test_compiler_accepts_data_type_strings_outside_agent_suggestions(self):
        document = deepcopy(self.single)
        values = document.pop("execution_inputs")
        document["workflow"]["steps"] = document["workflow"]["steps"][:1]
        document["workflow"]["steps"][0]["tools"] = document["workflow"]["steps"][0]["tools"][:1]
        values[0].update(value="custom_source", origin="user_answer")
        compiled = compile_workflow(document, workflow_id=str(uuid4()), execution_inputs=values)
        self.assertIn("custom_source", compiled["workflow"]["inputs"].values())
        document["workflow"]["steps"][0]["tools"][0]["arguments"]["data_type"] = {
            "source": "literal", "value": "custom_source"}
        compile_workflow(document, workflow_id=str(uuid4()))

    def test_tool_input_schema_uses_tool_type_and_preserves_prefill(self):
        document = deepcopy(self.single)
        values = document.pop("execution_inputs")
        values[0].update(value="NCE", origin="user_request")
        display, _ = input_display({"workflow_definition": document, "workflow_id": str(uuid4()),
                                    "workflow_revision": 1, "execution_inputs": values})
        parameter = next(p for p in display.tool_inputs[0].parameters if p.name == "data_type")
        self.assertEqual(parameter.value, "NCE")
        self.assertEqual(parameter.value_schema, {"type": "string"})
        validator = Draft202012Validator(parameter.value_schema)
        self.assertTrue(validator.is_valid("custom_source"))
        self.assertFalse(validator.is_valid(123))

    def setUp(self):
        self.deps = create_mock_dependencies()
        self.settings = replace(load_agent_settings({}), demo_artifacts_enabled=False,
                                workflow_generation_limit=1)
        self.state = {"user_request": "NCE 기반으로 WT fail 예측해줘",
                      "analysis_intent": {"intent": "failure_prediction"},
                      **{name: str(uuid4()) for name in ("task_id", "user_id", "project_id", "session_id")}}
        self.single = self.deps.workflow_agent.value.model_dump(mode="json")
        self.single["execution_inputs"] = [
            {"step_index": 0, "tool_index": 0, "parameter": "data_type", "value": "nce"}]
        self.requirements = [{"data_type": "nce", "role": "features"},
                             {"data_type": "wt_symbol", "role": "target"}]

    def dual_plan(self):
        plan = deepcopy(self.single)
        tools = plan["workflow"]["steps"][0]["tools"]
        tools.extend(deepcopy(tools))
        tools[3]["arguments"] = {"data": output(0, 2), "data_type": output(0, 2, "data_type")}
        plan["workflow"]["steps"] = [plan["workflow"]["steps"][0],
            {"skill": "dataset_preparation", "tools": [
                {"tool": "merge_data", "execution": "always", "arguments": {
                    "left_data": output(0, 1), "right_data": output(0, 3),
                    "join_columns": {"source": "input"}}}]},
            {"skill": "predictive_modeling", "tools": [
                {"tool": "split_dataset", "execution": "always", "arguments": {
                    "data": output(1, 0, "merged_data"), "target_column": {"source": "input"}}},
                {"tool": "train_xgboost", "execution": "always", "arguments": {
                    "train_data": output(2, 0, "train_data"), "test_data": output(2, 0, "test_data"),
                    "target_column": {"source": "input"}}}]}]
        plan["execution_inputs"].append({"step_index": 0, "tool_index": 2,
                                         "parameter": "data_type", "value": "wt_symbol"})
        return plan

    def generate(self, plans, requirements=None):
        agent = Mock()
        agent.ainvoke = AsyncMock(side_effect=plans)
        selector = Mock()
        selector.ainvoke = AsyncMock(return_value={
            "skill_names": ["data_load", "dataset_preparation", "predictive_modeling", "data_quality_check"],
            "dataset_requirements": self.requirements if requirements is None else requirements})
        deps = replace(self.deps, workflow_agent=agent, skill_selector_agent=selector)
        result = asyncio.run(make_generate_workflow(deps, self.settings)(self.state))
        return result, agent

    def test_dataset_requirements_are_forwarded_without_enforcing_tool_pairs(self):
        plan = deepcopy(self.single)
        plan["execution_inputs"] = []
        result, agent = self.generate([plan])
        agent.ainvoke.assert_called_once()
        self.assertEqual(agent.ainvoke.call_args.args[0]["dataset_requirements"], self.requirements)
        self.assertEqual(result["workflow_definition"]["workflow"]["steps"], plan["workflow"]["steps"])

    def test_two_source_plan_preserves_output_connections(self):
        result, agent = self.generate([self.dual_plan()])
        agent.ainvoke.assert_called_once()
        steps = result["workflow_definition"]["workflow"]["steps"]
        self.assertEqual(steps[1]["tools"][0]["arguments"]["right_data"], output(0, 3))
        self.assertEqual({v["value"] for v in result["execution_inputs"]}, {"nce", "wt_symbol"})

    def test_single_source_and_omitted_optional_arguments_stay_compact(self):
        result, agent = self.generate([self.single], [{"data_type": "nce", "role": "analysis"}])
        agent.ainvoke.assert_called_once()
        definition = result["workflow_definition"]["workflow"]
        statistics = definition["steps"][1]["tools"][1]
        self.assertEqual(set(statistics["arguments"]), {"data"})
        compiled = result["workflow"]["workflow"]["steps"][1]["tools"][1]
        self.assertIsNone(compiled["arguments"]["columns"])
        self.assertNotIn("step_1_tool_1_columns", result["workflow"]["workflow"]["inputs"])

    def test_raw_argument_values_receive_actionable_retry_feedback(self):
        for value in (None, "PT1H", 7, False, [], {}):
            with self.subTest(value=value):
                invalid = deepcopy(self.single)
                invalid["workflow"]["steps"][0]["tools"][0]["arguments"]["process"] = value
                result, agent = self.generate([invalid, self.single])
                self.assertEqual(agent.ainvoke.await_count, 2)
                feedback = agent.ainvoke.call_args.args[0]["validation_feedback"]
                self.assertIn("arguments.process", feedback)
                self.assertIn('use {"source":"input"}', feedback)
                self.assertIn("Omit unspecified optional arguments", feedback)
                self.assertIn("Do not generate null", feedback)
                self.assertEqual(result["workflow_definition"]["workflow"]["steps"],
                                 self.single["workflow"]["steps"])

    def test_omitted_optional_input_is_displayed_with_nullable_default(self):
        document = deepcopy(self.single)
        values = [{**value, "origin": "user_request"}
                  for value in document.pop("execution_inputs")]
        document["workflow"]["steps"][1]["tools"][1]["arguments"].pop("columns", None)
        display, _ = input_display({"workflow_definition": document,
            "workflow_id": str(uuid4()), "workflow_revision": 1, "execution_inputs": values})
        tool = next(t for t in display.tool_inputs if t.step_index == 1 and t.tool_index == 1)
        columns = next(p for p in tool.parameters if p.name == "columns")
        self.assertFalse(columns.required)
        self.assertTrue(columns.has_value)
        self.assertIsNone(columns.value)
        self.assertEqual(columns.value_origin, "default")
        self.assertTrue(Draft202012Validator(columns.value_schema).is_valid(None))

    def test_another_skill_is_allowed_between_extraction_pairs(self):
        document = deepcopy(self.single)
        values = document.pop("execution_inputs")
        # data_load pair -> data_quality_check -> another data_load pair.
        second = deepcopy(document["workflow"]["steps"][0])
        second["tools"][1]["arguments"] = {
            "data": output(2, 0), "data_type": output(2, 0, "data_type")}
        document["workflow"]["steps"].append(second)
        values.append({"step_index": 2, "tool_index": 0, "parameter": "data_type", "value": "nce"})
        compiled = compile_workflow(document, workflow_id=str(uuid4()), execution_inputs=values)
        self.assertEqual([s["skill"] for s in compiled["workflow"]["steps"]],
                         ["data_load", "data_quality_check", "data_load"])

    def test_intervening_skill_and_missing_transform_are_allowed(self):
        document = deepcopy(self.single)
        values = document.pop("execution_inputs")
        transform = document["workflow"]["steps"][0]["tools"].pop()
        for tool in document["workflow"]["steps"][1]["tools"]:
            tool["arguments"]["data"] = output(0, 0)
        document["workflow"]["steps"].append({"skill": "data_load", "tools": [transform]})
        compiled = compile_workflow(document, workflow_id=str(uuid4()), execution_inputs=values)
        self.assertEqual(len(compiled["workflow"]["steps"]), 3)
        document["workflow"]["steps"] = document["workflow"]["steps"][:1]
        compiled = compile_workflow(document, workflow_id=str(uuid4()), execution_inputs=values)
        self.assertEqual(len(compiled["workflow"]["steps"][0]["tools"]), 1)

    def test_invalid_output_reference_is_still_rejected(self):
        document = deepcopy(self.single)
        values = document.pop("execution_inputs")
        document["workflow"]["steps"][0]["tools"][1]["arguments"]["data"] = output(9, 0)
        with self.assertRaisesRegex(ValueError, "unknown or forward output reference"):
            compile_workflow(document, workflow_id=str(uuid4()), execution_inputs=values)


if __name__ == "__main__":
    unittest.main()
