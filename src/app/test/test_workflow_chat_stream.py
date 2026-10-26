"""Only validated Workflow JSON is published to the user-facing chat."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from uuid import uuid4

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langgraph.checkpoint.memory import InMemorySaver

from agent_config import load_agent_settings
from app.agents.orchestration.agent_interfaces import JsonMessageAgentAdapter
from app.agents.orchestration.mock_llm import create_mock_dependencies, override_sleep_seconds
from app.agents.workflow_generator.create_workflow_generator_agent import create_workflow_generator_agent
from app.graphs.builders.build_analysis_workflow_graph import build_analysis_workflow_graph
from app.graphs.nodes.generate_workflow import make_generate_workflow


class WorkflowChatStreamTests(unittest.TestCase):
    def setUp(self):
        sleep = override_sleep_seconds(0)
        sleep.__enter__()
        self.addCleanup(sleep.__exit__, None, None, None)
        self.deps = create_mock_dependencies()
        self.settings = replace(load_agent_settings({}), demo_artifacts_enabled=False)
        self.valid = self.deps.workflow_agent.value.model_dump(mode="json")

    def dependencies(self, responses):
        model = FakeListChatModel(responses=[json.dumps(value) for value in responses])
        return replace(self.deps, workflow_agent=JsonMessageAgentAdapter(
            create_workflow_generator_agent(model)))

    def test_retries_do_not_stream_raw_json_and_success_publishes_once(self):
        invalid = deepcopy(self.valid)
        invalid["workflow"]["steps"][0]["tools"][0]["arguments"]["unknown_filter"] = {"source": "input"}
        graph = build_analysis_workflow_graph(
            self.dependencies([invalid, invalid, self.valid]), self.settings,
            checkpointer=InMemorySaver(),
        )

        async def run():
            events = []
            async for event in graph.astream(
                {"user_request": "NCE 데이터 품질을 확인해줘"},
                {"configurable": {"thread_id": str(uuid4())}},
                stream_mode=["messages", "updates"],
            ):
                events.append(event)
            return events

        events = asyncio.run(run())
        tokens = [payload[0].content for mode, payload in events if mode == "messages"]
        self.assertFalse(any("workflow_version" in str(token) for token in tokens))
        self.assertFalse(tokens, "Intermediate model tokens must not reach the chat stream")
        final = [message for mode, update in events if mode == "updates"
                 for state in [update.get("generate_workflow")] if isinstance(state, dict)
                 for message in state.get("messages", [])
                 if message.get("name") == "workflow_generator"]
        self.assertEqual(len(final), 1)
        self.assertIn("후보", final[0]["content"])
        generated = [update["generate_workflow"] for mode, update in events
                     if mode == "updates" and "generate_workflow" in update]
        self.assertEqual(generated[0]["workflow_definition"], self.valid)
        self.assertTrue(final[0]["id"].startswith("workflow-"))

    def test_exhausted_retries_do_not_publish_an_invalid_workflow(self):
        invalid = deepcopy(self.valid)
        invalid["workflow"]["steps"][0]["tools"][0]["arguments"]["unknown_filter"] = {"source": "input"}
        node = make_generate_workflow(self.dependencies([invalid]), self.settings)
        with self.assertRaisesRegex(RuntimeError, "failed validation after retries"):
            asyncio.run(node({"user_request": "NCE 품질 확인", "task_id": str(uuid4()),
                  "analysis_intent": {"intent": "failure_prediction"}}))


if __name__ == "__main__":
    unittest.main()
