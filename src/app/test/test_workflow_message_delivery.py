"""Progress messages must reach display persistence as agent commentary."""
import unittest
from uuid import uuid4

from app.services.graph_event_persistence import (
    GraphPersistenceContext, GraphPersistenceCursor, extract_graph_events,
)
from app.services.chat_crud_message_sink import _content_text, _message_type


class WorkflowMessageDeliveryTests(unittest.TestCase):
    def test_progress_is_not_dropped_into_internal_logs_only(self):
        for name in ["workflow_generator", "workflow_candidate_collector", "workflow_input_resolver",
                     "executor", "rule_based_notebook_generator", "conditional_decision_agent"]:
            message = {"role": "agent", "name": name, "content": "작업을 준비하고 있습니다."}
            events, _ = extract_graph_events({"messages": [message]}, GraphPersistenceCursor(),
                context=GraphPersistenceContext(user_id=uuid4(), session_id="test"))
            with self.subTest(name=name):
                display = [e for e in events if e.kind == "display_message"]
                self.assertEqual(len(display), 1)
                self.assertEqual(display[0].display_message["role"], "agent")
                self.assertNotEqual(_message_type(message, agent_message_type="tool"), "assistant")
                self.assertEqual(_content_text(display[0].display_message["content"]), message["content"])

    def test_internal_messages_remain_internal(self):
        events, _ = extract_graph_events({"messages": [{"role": "agent", "name": "internal", "content": {"llm_run_id": "x"}}]},
            GraphPersistenceCursor(), context=GraphPersistenceContext(user_id=uuid4(), session_id="test"))
        self.assertFalse(any(e.kind == "display_message" for e in events))
