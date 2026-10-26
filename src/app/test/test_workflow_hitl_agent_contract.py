"""Agent contract builders, without graph execution, DB or LLM calls."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.graphs.hitl_protocol import build_hitl_request, request_human_input
from app.schemas.common.workflow_hitl_schema import (
    CandidateSelectionDisplay, ToolInputDisplay, PlanReviewDisplay,
    build_candidate_submission_schema, build_tool_input_submission_schema,
    build_plan_review_submission_schema,
)

EXAMPLES = Path(__file__).resolve().parents[3] / "docs/examples/workflow_hitl_v2"


class AgentHitlContractTests(unittest.TestCase):
    def fixture(self, name):
        return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))

    def display(self, name, model):
        fixture = self.fixture(name)
        arguments = fixture["sse_response"]["data"]["data"]["payload"]["arguments"]
        return model.model_validate(arguments)

    def test_documented_displays_roundtrip_and_requests_validate(self):
        cases = [
            ("01_recommended_selection.json", CandidateSelectionDisplay, build_candidate_submission_schema),
            ("01_generated_selection.json", CandidateSelectionDisplay, build_candidate_submission_schema),
            ("02_tool_inputs.json", ToolInputDisplay, build_tool_input_submission_schema),
            ("03_plan_review.json", PlanReviewDisplay, build_plan_review_submission_schema),
        ]
        for name, model, builder in cases:
            with self.subTest(name=name):
                fixture = self.fixture(name)
                args = fixture["sse_response"]["data"]["data"]["payload"]["arguments"]
                display = model.model_validate(args)
                self.assertEqual(display.model_dump(mode="json"), args)
                schema = builder(display)
                Draft202012Validator.check_schema(schema)
                validator = Draft202012Validator(schema)
                for request in fixture["resume_requests"].values():
                    resume = request["body"]["command"]["resume"]
                    if resume["action"] == "edit":
                        validator.validate(resume["payload"])
                self.assertEqual(display.model_dump(mode="json"), args)

    def test_documented_output_edit_reaches_approval_and_schemas_match(self):
        fixture = self.fixture("02_tool_inputs.json")
        display = self.display("02_tool_inputs.json", ToolInputDisplay)
        schema = build_tool_input_submission_schema(display)
        raw = fixture["rest_response"]["body"]["current_run"]["interrupt"][0]
        public = fixture["sse_response"]["data"]["data"]
        self.assertEqual(raw["review_configs"][0]["args_schema"], schema)
        self.assertEqual(public["payload"]["input_schema"], schema)
        self.assertEqual(public["actions"][0]["input_schema"], schema)
        self.assertTrue(display.tool_inputs[1].parameters[0].editable)
        self.assertTrue(display.output_catalog)
        self.assertTrue(all("output_options" not in p for t in display.model_dump()["tool_inputs"] for p in t["parameters"]))
        selected = fixture["resume_requests"]["change_output_reference"]["body"]["command"]["resume"]["payload"]
        item = next(i for i in selected["inputs"] if i["parameter"] == "data")
        approval = self.display("03_plan_review.json", PlanReviewDisplay)
        binding = approval.workflow["workflow"]["steps"][1]["tools"][0]["arguments"]["data"]
        self.assertEqual(binding, {"source": "tool_output", **item["reference"]})
        self.assertFalse(any(i.parameter == "data" for i in approval.execution_inputs))

    def test_candidate_constraints_and_optional_name(self):
        display = self.display("01_recommended_selection.json", CandidateSelectionDisplay)
        display.candidates[0].name = None
        self.assertNotIn("name", display.model_dump()["candidates"][0])
        validator = Draft202012Validator(build_candidate_submission_schema(display))
        for payload in [
            {"intent": "select", "candidate_id": "unknown"},
            {"intent": "regenerate"},
            {"intent": "reject", "reason": "  \n"},
            {"intent": "reject", "reason": "stop", "candidate_id": "recommended_1"},
        ]:
            self.assertFalse(validator.is_valid(payload))
        validator.validate({"intent": "recommend_again"})
        validator.validate({"intent": "recommend_again", "feedback": "later"})
        display.candidates = []
        with self.assertRaises(ValidationError):
            build_candidate_submission_schema(display)
        display.available_actions = [a for a in display.available_actions if a.intent != "select"]
        Draft202012Validator.check_schema(build_candidate_submission_schema(display))

    def test_generated_requests_pass_api_projection_and_resume(self):
        from types import SimpleNamespace
        from uuid import UUID
        from app.schemas.common.workflow_run_schema import WorkflowRunRequest
        from app.services.workflow_interactions import public_interactions
        from app.services.workflow_run_service import resume_command

        cases = [
            ("01_recommended_selection.json", CandidateSelectionDisplay, build_candidate_submission_schema),
            ("01_generated_selection.json", CandidateSelectionDisplay, build_candidate_submission_schema),
            ("02_tool_inputs.json", ToolInputDisplay, build_tool_input_submission_schema),
            ("03_plan_review.json", PlanReviewDisplay, build_plan_review_submission_schema),
        ]
        for filename, model, builder in cases:
            fixture = self.fixture(filename)
            original = fixture["sse_response"]["data"]["data"]
            display = model.model_validate(original["payload"]["arguments"])
            request = build_hitl_request(
                action_name=original["payload"]["action_name"], description=original["summary"],
                args=display.model_dump(mode="json"), args_schema=builder(display),
                allowed_decisions=[a["name"] for a in original["actions"]], payload_version=2,
            )
            run = SimpleNamespace(run_id=UUID(original["run_id"]),
                                  task_id=UUID(original["resume_target"]["task_id"]), interrupt=[request])
            public = public_interactions(run)[0]
            self.assertEqual(public["payload_version"], 2)
            self.assertEqual(public["payload"]["arguments"], original["payload"]["arguments"])
            for example in fixture["resume_requests"].values():
                body = deepcopy(example["body"])
                body["command"]["resume"]["interaction_id"] = public["interaction_id"]
                resume = WorkflowRunRequest.model_validate(body).command.resume
                decision = resume_command(run, resume)["decisions"][0]
                if resume.action == "edit":
                    self.assertEqual(decision["edited_action"]["args"], resume.payload)
                else:
                    self.assertEqual(decision, {"type": "approve"})

    def test_tool_schema_rejects_bad_addresses_and_values(self):
        display = self.display("02_tool_inputs.json", ToolInputDisplay)
        validator = Draft202012Validator(build_tool_input_submission_schema(display))
        payload = self.fixture("02_tool_inputs.json")["resume_requests"]["submit_all"]["body"]["command"]["resume"]["payload"]
        mutations = [
            lambda p: p["inputs"].pop(0),
            lambda p: p["inputs"].append(deepcopy(p["inputs"][0])),
            lambda p: p["inputs"][2].update(parameter="data", value={}),
            lambda p: p["inputs"][0].update(step_index=8),
            lambda p: p["inputs"][0].update(step_index=True),
            lambda p: p["inputs"][2].update(value=None),
            lambda p: p.update(workflow_revision=2),
        ]
        for mutate in mutations:
            bad = deepcopy(payload)
            mutate(bad)
            self.assertFalse(validator.is_valid(bad), bad)

    def test_parameter_presence_and_readonly_validation(self):
        display = self.display("02_tool_inputs.json", ToolInputDisplay).model_dump()
        self.assertNotIn("value", display["tool_inputs"][0]["parameters"][0])
        self.assertIsNone(display["tool_inputs"][1]["parameters"][3]["value"])
        for mutation in ("missing_null", "missing_binding", "no_default", "duplicate"):
            bad = deepcopy(display)
            if mutation == "missing_null":
                bad["tool_inputs"][1]["parameters"][3].pop("value")
            elif mutation == "missing_binding":
                bad["tool_inputs"][1]["parameters"][0].pop("binding")
            elif mutation == "no_default":
                p = bad["tool_inputs"][1]["parameters"][3]
                p["has_default"] = False
                p.pop("default_value")
            else:
                bad["tool_inputs"].append(deepcopy(bad["tool_inputs"][0]))
            with self.assertRaises(ValidationError):
                ToolInputDisplay.model_validate(bad)

    def test_empty_inputs_and_nested_parameter_schema(self):
        display = self.display("02_tool_inputs.json", ToolInputDisplay)
        display.tool_inputs = []
        validator = Draft202012Validator(build_tool_input_submission_schema(display))
        validator.validate({"intent": "submit_inputs", "workflow_id": "workflow_1", "workflow_revision": 1, "inputs": []})
        display = self.display("02_tool_inputs.json", ToolInputDisplay)
        p = display.tool_inputs[0].parameters[0]
        p.value_schema = {"$defs": {"ref": {"type": "string", "pattern": "^dataset_"}}, "$ref": "#/$defs/ref"}
        validator = Draft202012Validator(build_tool_input_submission_schema(display))
        payload = self.fixture("02_tool_inputs.json")["resume_requests"]["submit_all"]["body"]["command"]["resume"]["payload"]
        validator.validate(payload)
        payload["inputs"][0]["value"] = "wrong"
        self.assertFalse(validator.is_valid(payload))

    def test_helper_version_and_resume_compatibility(self):
        kwargs = dict(action_name="sample", description="review", args={"x": 1})
        legacy = build_hitl_request(**kwargs)
        self.assertNotIn("payload_version", legacy)
        self.assertEqual(legacy, build_hitl_request(**kwargs, payload_version=1))
        self.assertEqual(build_hitl_request(**kwargs, payload_version=2), {**legacy, "payload_version": 2})
        for version in (True, "2", 0, 3):
            with self.assertRaises(ValueError):
                build_hitl_request(**kwargs, payload_version=version)
        raw = {"decisions": [{"type": "edit", "edited_action": {"args": {"intent": "reject", "reason": "stop"}}}]}
        with patch("app.graphs.hitl_protocol.interrupt", return_value=raw) as interrupt:
            self.assertEqual(request_human_input(**kwargs, payload_version=2), {"intent": "reject", "reason": "stop"})
            self.assertEqual(interrupt.call_args.args[0]["payload_version"], 2)
        with patch("app.graphs.hitl_protocol.interrupt", return_value={"decisions": [{"type": "approve"}]}):
            self.assertEqual(request_human_input(**kwargs), {"x": 1})


if __name__ == "__main__":
    unittest.main()
