"""Registry changes propagate to display, schema, routing and CLI."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from app.schemas.common import workflow_hitl_actions as registry
from app.schemas.common.workflow_hitl_schema import build_candidate_submission_schema
from app.graphs.nodes.candidate_contract import candidate_display
from app.graphs.routers.orchestration_router import route_workflow_candidate_selection


class ActionRegistryTests(unittest.TestCase):
    def display(self):
        return candidate_display([
            {"id": "one", "origin": "recommended", "workflow": {"workflow": {"steps": []}}}
        ], can_regenerate=True)

    def test_visibility_change_updates_display_and_submission_schema(self):
        actions = tuple(replace(a, origins=("generated",))
                        if (a.scope, a.intent) == ("candidate", "generate") else a
                        for a in registry.ACTIONS)
        with patch.object(registry, "ACTIONS", actions):
            display = self.display()
            self.assertNotIn("generate", [a.intent for a in display.available_actions])
            validator = Draft202012Validator(build_candidate_submission_schema(display))
            self.assertFalse(validator.is_valid({"intent": "generate"}))
            self.assertTrue(validator.is_valid({"intent": "select", "candidate_id": "one"}))

    def test_label_change_reaches_display(self):
        actions = tuple(replace(a, label="새 워크플로우 만들기")
                        if (a.scope, a.intent) == ("candidate", "generate") else a
                        for a in registry.ACTIONS)
        with patch.object(registry, "ACTIONS", actions):
            labels = {a.intent: a.label for a in self.display().available_actions}
            self.assertEqual(labels["generate"], "새 워크플로우 만들기")

    def test_registered_handler_and_destination_drive_generation(self):
        answer = registry.CANDIDATE_SUBMISSION.validate_python({"intent": "generate"})
        outcome = registry.dispatch_candidate({}, answer)
        self.assertEqual(outcome.updates["routing_context"], "main")
        self.assertEqual(route_workflow_candidate_selection({
            "workflow_candidate_selection": answer.model_dump()
        }), registry.get_action("candidate", "generate").destination)

    def test_cli_uses_registered_required_fields_and_available_actions(self):
        from cli import _prompt_resume_value

        payload = {"kind": "workflow_candidate_selection", **self.display().model_dump()}
        for inputs, expected in [
            (["generate"], {"intent": "generate"}),
            (["reject", "분석 중단"], {"intent": "reject", "reason": "분석 중단"}),
            (["1"], {"intent": "select", "candidate_id": "one"}),
        ]:
            with self.subTest(inputs=inputs), patch("builtins.input", side_effect=inputs), patch("builtins.print"):
                self.assertEqual(_prompt_resume_value(payload), expected)
        payload["available_actions"] = [{"intent": "reject", "label": "거절"}]
        with patch("builtins.input", return_value="1"), patch("builtins.print"):
            with self.assertRaises(ValueError):
                _prompt_resume_value(payload)
