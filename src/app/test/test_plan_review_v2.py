"""Approval transport cannot edit or implicitly approve the displayed plan."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from jsonschema import ValidationError
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, END, StateGraph
from langgraph.types import Command

from app.graphs.nodes.hitl import review_workflow
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.schemas.common.workflow_run_schema import WorkflowRunRequest
from app.services.workflow_interactions import public_interactions
from app.services.workflow_run_service import resume_command


class PlanReviewV2Tests(unittest.TestCase):
    def state(self):
        workflow_id = str(uuid4())
        return {"workflow_id": workflow_id, "workflow_revision": 1, "workflow_status": "ready",
                "workflow_origin": "generated", "tool_inputs_confirmed": True, "plan_review_payload_version": 2,
                "workflow": {"schema_version": "1.3", "workflow": {"id": workflow_id, "steps": []}},
                "execution_inputs": [{"step_index": 0, "tool_index": 0, "parameter": "limit",
                                      "value": 0, "origin": "user_answer", "confirmed": True}]}

    def test_approve_preserves_exact_workflow_and_inputs(self):
        state = self.state()
        before = deepcopy(state)
        with patch("app.graphs.nodes.plan_review_v2.interrupt", return_value={"decisions": [{"type": "approve"}]}) as interrupt:
            result = review_workflow(state)
        request = interrupt.call_args.args[0]
        self.assertEqual(request["payload_version"], 2)
        self.assertEqual(request["review_configs"][0]["allowed_decisions"], ["approve", "edit"])
        args = request["action_requests"][0]["args"]
        self.assertEqual(args["workflow"], state["workflow"])
        self.assertEqual(args["execution_inputs"][0]["value"], 0)
        self.assertNotIn("origin", args["execution_inputs"][0])
        self.assertEqual(result["approval"], {"approved": True, "feedback": None})
        self.assertNotIn("workflow", result)
        self.assertNotIn("execution_inputs", result)
        self.assertEqual(state, before)

    def test_reason_rejection_routes_without_assuming_cancellation(self):
        answer = {"decisions": [{"type": "edit", "edited_action": {
            "name": "workflow_approval", "args": {"intent": "reject", "reason": "도구 구성을 바꿔줘"}}}]}
        with patch("app.graphs.nodes.plan_review_v2.interrupt", return_value=answer):
            result = review_workflow(self.state())
        self.assertFalse(result["approval"]["approved"])
        self.assertEqual(result["user_request"], "도구 구성을 바꿔줘")
        self.assertEqual(result["routing_context"], "workflow_rejected")
        self.assertNotIn("cancel_reason", result)

    def test_invalid_or_modified_approval_and_empty_reason_are_rejected(self):
        for response in [True, {"approved": True}, {"intent": "approve"},
                         {"intent": "reject", "reason": "  "},
                         {"intent": "reject", "reason": "수정", "workflow": {}},
                         {"decisions": [{"type": "approve", "payload": {"limit": 1}}]},
                         {"decisions": [{"type": "reject", "message": "거절"}]},
                         {"decisions": [{"type": "edit", "edited_action": {"name": "other", "args": {}}}]},
                         {"decisions": [{"type": "edit", "edited_action": {"name": "workflow_approval", "args": self.state()["workflow"]}}]}]:
            with self.subTest(response=response), patch("app.graphs.nodes.plan_review_v2.interrupt", return_value=response):
                with self.assertRaises((ValueError, ValidationError)):
                    review_workflow(self.state())

    def test_unconfirmed_inputs_cannot_open_review(self):
        with patch("app.graphs.nodes.plan_review_v2.interrupt") as interrupt:
            with self.assertRaises(ValueError):
                review_workflow({**self.state(), "tool_inputs_confirmed": False})
            interrupt.assert_not_called()

    def test_api_projection_and_checkpoint_resume_for_both_actions(self):
        for action, payload in [("approve", None), ("edit", {"intent": "reject", "reason": "다시 구성"})]:
            with self.subTest(action=action):
                builder = StateGraph(AnalysisWorkflowState)
                builder.add_node("review", review_workflow)
                builder.add_edge(START, "review")
                builder.add_edge("review", END)
                saver = InMemorySaver()
                graph = builder.compile(checkpointer=saver)
                config = {"configurable": {"thread_id": str(uuid4())}}
                request = graph.invoke(self.state(), config)["__interrupt__"][0].value
                run = SimpleNamespace(run_id=uuid4(), task_id=uuid4(), interrupt=[request])
                public = public_interactions(run)[0]
                args = {"interaction_id": public["interaction_id"], "revision": 1, "action": action}
                if payload is not None:
                    args["payload"] = payload
                resume = WorkflowRunRequest.model_validate({"run_id": str(run.run_id), "task_id": str(run.task_id),
                                                            "command": {"resume": args}}).command.resume
                result = builder.compile(checkpointer=saver).invoke(Command(resume=resume_command(run, resume)), config)
                self.assertEqual(result["approval"]["approved"], action == "approve")

    def test_old_pending_review_stays_v1(self):
        state = self.state()
        del state["plan_review_payload_version"]
        with patch("app.graphs.nodes.hitl.request_human_input", return_value=True) as request:
            result = review_workflow(state)
        self.assertNotIn("payload_version", request.call_args.kwargs)
        self.assertTrue(result["approval"]["approved"])

    def test_cli_uses_approve_and_edit_transport(self):
        from cli import _prompt_resume_value
        state = self.state()
        with patch("app.graphs.nodes.plan_review_v2.interrupt", return_value={"decisions": [{"type": "approve"}]}) as interrupt:
            review_workflow(state)
        display = {"kind": "workflow_approval", **interrupt.call_args.args[0]["action_requests"][0]["args"]}
        for inputs in [["approve"], ["reject", "도구 변경"]]:
            with patch("builtins.input", side_effect=inputs), patch("builtins.print"):
                raw = _prompt_resume_value(display)
            with patch("app.graphs.nodes.plan_review_v2.interrupt", return_value=raw):
                result = review_workflow(state)
            self.assertEqual(result["approval"]["approved"], inputs[0] == "approve")
