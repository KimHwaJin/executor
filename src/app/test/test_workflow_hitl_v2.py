"""Executable API boundary contract; no database, LLM or executor calls."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from uuid import UUID, uuid4
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.v1.routes import workflow_run as route
from app.schemas.common.workflow_run_schema import WorkflowRunRequest
from app.schemas.common.workflow_task_schema import TaskSnapshot
from app.services.workflow_interactions import public_interactions
from app.core.enums import AgentRunStatus
from app.services import workflow_run_service as service
from app.services.workflow_run_service import resume_command

EXAMPLES = Path(__file__).resolve().parents[3] / "docs/examples/workflow_hitl_v2"


class WorkflowHitlV2Tests(unittest.TestCase):
    def fixture(self, filename="02_tool_inputs.json", request="submit_all"):
        data = json.loads((EXAMPLES / filename).read_text())
        body = data["rest_response"]["body"]
        run = SimpleNamespace(run_id=UUID(body["current_run_id"]), task_id=UUID(body["task_id"]),
                              interrupt=deepcopy(body["current_run"]["interrupt"]))
        public = public_interactions(run)[0]
        request_body = deepcopy(data["resume_requests"][request]["body"])
        request_body["command"]["resume"]["interaction_id"] = public["interaction_id"]
        return data, run, WorkflowRunRequest.model_validate(request_body).command.resume

    def assert_rejected(self, run, resume, code=422):
        with self.assertRaises(HTTPException) as caught:
            resume_command(run, resume)
        self.assertEqual(caught.exception.status_code, code)
        return caught.exception

    def test_all_documented_requests_and_rest_sse_projection(self):
        for file in EXAMPLES.glob("*.json"):
            data = json.loads(file.read_text())
            if "resume_requests" not in data:
                # Terminal-result fixtures have no pending interaction to resume.
                self.assertTrue(data["rest_response"]["body"]["task_finished"])
                continue
            for key in data["resume_requests"]:
                with self.subTest(file=file.name, request=key):
                    data, run, resume = self.fixture(file.name, key)
                    actual = public_interactions(run)[0]
                    expected = deepcopy(data["rest_response"]["body"]["interactions"][0])
                    # Documentation IDs were placeholders; server IDs are deterministic.
                    expected["interaction_id"] = actual["interaction_id"]
                    expected["resume_target"]["interaction_id"] = actual["interaction_id"]
                    self.assertEqual(actual, expected)
                    self.assertEqual(data["sse_response"]["data"]["data"],
                                     data["rest_response"]["body"]["interactions"][0])
                    command = resume_command(run, resume)["decisions"][0]
                    if resume.action == "edit":
                        self.assertEqual(command["edited_action"]["args"], resume.payload)
                        self.assertEqual(command["type"], "edit")
                    else:
                        self.assertEqual(command, {"type": "approve"})

    def test_stale_interaction_and_revision(self):
        _, run, resume = self.fixture()
        resume.revision = 2
        self.assert_rejected(run, resume, 409)
        resume.revision = 1
        resume.interaction_id = uuid4()
        self.assert_rejected(run, resume, 409)

    def test_workflow_revision_precedes_schema_and_invalid_types_are_422(self):
        for revision, status in [(2, 409), (0, 422), (True, 422), ("1", 422), (None, 422)]:
            with self.subTest(revision=revision):
                _, run, resume = self.fixture()
                resume.payload["workflow_revision"] = revision
                self.assert_rejected(run, resume, status)

    def test_tool_input_rejections(self):
        mutations = [
            lambda p: p.update(workflow_id="other"),
            lambda p: p["inputs"].append(deepcopy(p["inputs"][0])),
            lambda p: p["inputs"].pop(0),
            lambda p: p["inputs"][0].update(step_index=999),
            lambda p: p["inputs"][0].update(step_index=True),
            lambda p: p["inputs"][0].update(value=123),
            lambda p: p["inputs"][0].update(parameter="data", step_index=1, value={}),
            lambda p: p.update(unexpected=True),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                _, run, resume = self.fixture()
                mutate(resume.payload)
                self.assert_rejected(run, resume)

    def test_domain_validation_does_not_rely_on_schema_enum(self):
        _, run, resume = self.fixture("01_recommended_selection.json", "select")
        run.interrupt[0]["review_configs"][0]["args_schema"] = {}
        resume.payload["candidate_id"] = "invented"
        self.assert_rejected(run, resume)
        resume.payload = {"intent": "regenerate"}
        self.assert_rejected(run, resume)

    def test_rejection_reason_and_transport(self):
        for file in ["01_recommended_selection.json", "03_plan_review.json"]:
            for reason in ["", "   ", None, 42]:
                _, run, resume = self.fixture(file, "reject_new_workflow")
                resume.payload["reason"] = reason
                self.assert_rejected(run, resume)
        _, run, resume = self.fixture("03_plan_review.json", "approve")
        resume.payload = {"intent": "reject", "reason": "stop"}
        self.assert_rejected(run, resume)
        resume.action = "reject"
        self.assert_rejected(run, resume)

    def test_v1_default_alias_json_feedback_and_reject_are_compatible(self):
        _, run, resume = self.fixture("03_plan_review.json", "reject_new_workflow")
        item = run.interrupt[0]
        item.pop("payload_version")
        item["review_configs"][0].update(allowed_decisions=["approve", "edit", "reject"], args_schema={})
        self.assertEqual(public_interactions(run)[0]["payload_version"], 1)
        resume.action, resume.payload, resume.feedback = "workflow_approval", None, '{"approved":false}'
        self.assertEqual(resume_command(run, resume)["decisions"][0]["edited_action"]["args"], {"approved": False})
        resume.action, resume.feedback = "reject", "수정 요청"
        self.assertEqual(resume_command(run, resume)["decisions"][0], {"type": "reject", "message": "수정 요청"})

    def test_v2_does_not_accept_legacy_alias_or_json_feedback(self):
        _, run, resume = self.fixture()
        resume.action = "additional_information"
        self.assert_rejected(run, resume)
        resume.action, resume.feedback, resume.payload = "edit", json.dumps(resume.payload), None
        self.assert_rejected(run, resume)

    def test_unknown_version_does_not_resume(self):
        _, run, resume = self.fixture()
        run.interrupt[0]["payload_version"] = 3
        self.assert_rejected(run, resume, 409)

    def test_invalid_resume_never_creates_a_run(self):
        data, run, resume = self.fixture()
        run.session_id = uuid4()
        run.status = AgentRunStatus.INTERRUPTED
        payload = WorkflowRunRequest.model_validate(data["resume_requests"]["submit_all"]["body"])
        payload.command.resume = resume
        resume.payload["workflow_revision"] = 2
        db = SimpleNamespace(scalar=AsyncMock(side_effect=[object(), None, run]))
        with patch.object(service, "owned_run", AsyncMock(return_value=run)), \
             patch.object(service.SessionRepository, "get_active_by_user", AsyncMock()), \
             patch.object(service.RunService, "create", AsyncMock()) as create:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(service.submit_workflow(payload, "new-key", uuid4(), db))
            self.assertEqual(caught.exception.status_code, 409)
            create.assert_not_awaited()

    def test_zero_false_null_and_omission_are_preserved(self):
        for value, schema in [(0, {"type": "number"}), (False, {"type": "boolean"}),
                              (None, {"type": "null"})]:
            _, run, resume = self.fixture()
            run.interrupt[0]["review_configs"][0]["args_schema"] = {}
            run.interrupt[0]["action_requests"][0]["args"]["tool_inputs"][1]["parameters"][3]["value_schema"] = schema
            next(i for i in resume.payload["inputs"] if i["parameter"] == "threshold")["value"] = value
            actual = resume_command(run, resume)["decisions"][0]["edited_action"]["args"]
            self.assertIs(next(i for i in actual["inputs"] if i["parameter"] == "threshold")["value"], value)
        _, run, resume = self.fixture(request="legacy_values_only")
        resume.payload["inputs"] = [i for i in resume.payload["inputs"] if i["parameter"] not in ("method", "threshold")]
        self.assertEqual(len(resume_command(run, resume)["decisions"][0]["edited_action"]["args"]["inputs"]), 2)
        _, run, resume = self.fixture(request="clear_optional_to_defaults")
        inputs = resume_command(run, resume)["decisions"][0]["edited_action"]["args"]["inputs"]
        self.assertEqual({i["parameter"] for i in inputs if i["source"] == "default"}, {"method", "threshold"})

    def test_rest_router_202_and_invalid_sse_cursor_before_submission(self):
        data, run, _ = self.fixture()
        app = FastAPI()
        app.include_router(route.router, prefix="/api/v1")
        app.dependency_overrides[route.workflow_auth] = lambda: uuid4()
        app.dependency_overrides[route.get_db] = lambda: None
        app.dependency_overrides[route.submission] = lambda: run
        body = data["resume_requests"]["submit_all"]["body"]
        data["rest_response"]["body"]["current_run"]["run_id"] = str(run.run_id)
        with patch.object(route, "task_snapshot", AsyncMock(return_value=TaskSnapshot.model_validate(
                data["rest_response"]["body"]))), TestClient(app) as client:
            response = client.post("/api/v1/workflow_run", json=body)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["submission_run_id"], str(run.run_id))
            self.assertNotIn("main_model_name", response.json())
            self.assertNotIn("main_model", response.json())
            self.assertEqual(response.headers["location"], f"/api/v1/tasks/{run.task_id}")
            with patch.object(route, "submit_workflow", AsyncMock()) as submit:
                response = client.post("/api/v1/workflow_run/stream", json=body,
                                       headers={"Last-Event-ID": "-1"})
                self.assertEqual(response.status_code, 400)
                submit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
