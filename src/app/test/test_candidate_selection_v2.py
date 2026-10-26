"""Candidate selection can be wired without a recommendation backend."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from uuid import uuid4

from jsonschema import ValidationError
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.graphs.nodes.candidate_contract import candidate_display, candidate_fingerprint
from app.graphs.nodes.hitl import make_select_workflow_candidate
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.schemas.common.workflow_run_schema import WorkflowRunRequest
from app.services.workflow_interactions import public_interactions
from app.services.workflow_run_service import resume_command


class CandidateSelectionV2Tests(unittest.TestCase):
    def candidate(self, index=1, origin="recommended"):
        return {"id": f"{origin}_{index}", "origin": origin, "workflow_id": f"workflow_{index}",
                "metadata": {"reason": "test recommendation"},
                "workflow": {"workflow": {"name": f"Candidate {index}", "steps": [], "status": "needs_input"}}}

    def start(self, *, origin="recommended", version=2, revision=1):
        graph = StateGraph(AnalysisWorkflowState)
        graph.add_node("select", make_select_workflow_candidate(SimpleNamespace(
            max_workflow_revisions=3, workflow_recommendation_enabled=False)))
        graph.add_edge(START, "select")
        graph.add_edge("select", END)
        saver = InMemorySaver()
        compiled = graph.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": str(uuid4())}}
        state = {"workflow_candidates": [self.candidate(i, origin) for i in range(1, 4 if origin == "recommended" else 2)],
                 "user_request": "Original analysis request", "workflow_revision": revision}
        if version is not None:
            state["candidate_selection_payload_version"] = version
        result = compiled.invoke(state, config)
        return compiled, config, result, graph, saver

    def test_injected_recommendations_select_through_api_and_checkpoint(self):
        graph, config, first, _, _ = self.start()
        interrupt = first["__interrupt__"][0].value
        args = interrupt["action_requests"][0]["args"]
        self.assertEqual([a["intent"] for a in args["available_actions"]], ["select", "generate", "reject"])
        self.assertEqual(len(args["candidates"]), 3)
        run = SimpleNamespace(run_id=uuid4(), task_id=uuid4(), interrupt=[interrupt])
        public = public_interactions(run)[0]
        resume = WorkflowRunRequest.model_validate({
            "task_id": str(run.task_id), "run_id": str(run.run_id),
            "command": {"resume": {"interaction_id": public["interaction_id"], "revision": 1,
                                     "action": "edit", "payload": {"intent": "select", "candidate_id": "recommended_2"}}},
        }).command.resume
        result = graph.invoke(Command(resume=resume_command(run, resume)), config)
        self.assertEqual(result["workflow_id"], "workflow_2")
        self.assertEqual(result["workflow_origin"], "recommended")
        self.assertEqual(result["workflow_status"], "needs_input")

    def test_old_checkpoint_without_version_still_accepts_v1(self):
        _, config, first, builder, saver = self.start(version=None)
        self.assertNotIn("payload_version", first["__interrupt__"][0].value)
        # Recompile against the saved checkpoint, as on an application restart.
        graph = builder.compile(checkpointer=saver)
        result = graph.invoke(Command(resume={"candidate_number": 2}), config)
        self.assertEqual(result["workflow_id"], "workflow_2")

    def agent_chat_resume(self, first, submission):
        args = deepcopy(first["__interrupt__"][0].value["action_requests"][0]["args"])
        args["available_actions"] = submission
        return {"decisions": [{"type": "edit", "edited_action": {
            "name": "workflow_candidate_selection", "args": args}}]}

    def test_agent_chat_selects_candidate_from_available_actions(self):
        for origin, candidate_id in [("recommended", "recommended_2"), ("generated", "generated_1")]:
            for as_text in (False, True):
                with self.subTest(origin=origin, as_text=as_text):
                    graph, config, first, _, _ = self.start(origin=origin)
                    submission = {"intent": "select", "candidate_id": candidate_id}
                    raw = self.agent_chat_resume(first, json.dumps(submission) if as_text else submission)
                    # Display data is never used as the selected workflow definition.
                    raw["decisions"][0]["edited_action"]["args"]["candidates"] = []
                    result = graph.invoke(Command(resume=raw), config)
                    self.assertEqual(result["workflow_candidate_selection"], submission)
                    self.assertEqual(result["workflow_id"], "workflow_2" if origin == "recommended" else "workflow_1")

    def test_agent_chat_actions_use_existing_dispatch(self):
        for submission in [{"intent": "regenerate", "feedback": "try again"},
                           {"intent": "reject", "reason": "change request"}]:
            graph, config, first, _, _ = self.start(origin="generated")
            result = graph.invoke(Command(resume=self.agent_chat_resume(first, submission)), config)
            self.assertEqual(result["workflow_candidate_selection"], submission)
            self.assertEqual(result["approval_feedback"], submission.get("feedback", submission.get("reason")))

    def test_agent_chat_cannot_bypass_candidate_submission_validation(self):
        for submission in [{"intent": "select", "candidate_id": "missing"},
                           {"intent": "select", "candidate_id": "generated_1", "extra": True},
                           {"intent": "generate"}, {"intent": "reject", "reason": "   "}]:
            graph, config, first, _, _ = self.start(origin="generated")
            with self.assertRaises(ValidationError):
                graph.invoke(Command(resume=self.agent_chat_resume(first, submission)), config)

    def test_generated_actions_and_revision_limit(self):
        for revision, expected in [(1, ["select", "regenerate", "reject"]), (3, ["select", "reject"])]:
            _, _, result, _, _ = self.start(origin="generated", revision=revision)
            args = result["__interrupt__"][0].value["action_requests"][0]["args"]
            self.assertEqual([a["intent"] for a in args["available_actions"]], expected)
        # Contract supports the future recommendation route, but the current
        # node deliberately does not turn that capability on.
        display = candidate_display([self.candidate()], can_regenerate=True, can_recommend_again=True)
        self.assertEqual([a.intent for a in display.available_actions], ["select", "recommend_again", "generate", "reject"])

    def test_recommended_generate_routes_without_excluding_recommendations(self):
        from app.graphs.routers.orchestration_router import route_workflow_candidate_selection
        from app.graphs.nodes.flow_control import announce_workflow_search
        graph, config, _, _, _ = self.start()
        result = graph.invoke(Command(resume={"intent": "generate"}), config)
        self.assertEqual(route_workflow_candidate_selection(result), "announce_workflow_search")
        self.assertEqual(result["user_request"], "Original analysis request")
        self.assertFalse(result.get("excluded_workflow_candidates"))
        self.assertEqual(announce_workflow_search(result)["workflow_candidates"], [])
        for origin, revision in [("generated", 1), ("recommended", 3)]:
            graph, config, _, _, _ = self.start(origin=origin, revision=revision)
            with self.assertRaises(ValidationError):
                graph.invoke(Command(resume={"intent": "generate"}), config)

    def test_invalid_direct_resume_does_not_bypass_contract(self):
        for payload in [{"intent": "select", "candidate_id": "missing"},
                        {"intent": "recommend_again"}, {"intent": "regenerate"},
                        {"intent": "reject", "reason": "   "}, {"candidate_number": 1}]:
            graph, config, _, _, _ = self.start()
            with self.assertRaises(ValidationError):
                graph.invoke(Command(resume=payload), config)

    def test_exclusion_ignores_cosmetic_names(self):
        doc = self.candidate()["workflow"]
        other = deepcopy(doc)
        other["workflow"].update(name="Renamed", actor={"type": "AGENT", "id": "other"})
        self.assertEqual(candidate_fingerprint(doc), candidate_fingerprint(other))
        display = candidate_display([self.candidate(origin="generated"), self.candidate(2, "generated")], can_regenerate=True)
        self.assertEqual(len(display.candidates), 2)


if __name__ == "__main__":
    unittest.main()
