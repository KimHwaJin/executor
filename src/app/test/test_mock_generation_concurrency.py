"""Exercise the real generation node, including typed conversion and async compilation."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import unittest
from unittest.mock import patch
from uuid import uuid4

from agent_config import load_agent_settings
from app.agents.orchestration.mock_llm import create_mock_dependencies, quality_plan, override_sleep_seconds
from app.agents.workflow_generator import workflow_standard_compiler as compiler
from app.graphs.nodes.generate_workflow import make_generate_workflow


class MockGenerationTests(unittest.TestCase):
    def test_shared_mock_dependencies_150_concurrent_generations(self):
        node = make_generate_workflow(create_mock_dependencies(),
            replace(load_agent_settings({}), demo_artifacts_enabled=False, workflow_generation_limit=1))

        async def run():
            with override_sleep_seconds(0):
                return await asyncio.gather(*(node({
                    'task_id': str(uuid4()), 'user_id': str(uuid4()),
                    'project_id': str(uuid4()), 'session_id': str(uuid4()),
                    'user_request': 'NCE 데이터 품질을 확인해줘',
                    'analysis_intent': {'intent': 'failure_prediction'},
                }) for _ in range(150)))

        results = asyncio.run(run())
        self.assertEqual(len(results), 150)
        for result in results:
            self.assertEqual(result['workflow_status'], 'needs_input')
            tools = result['workflow']['workflow']['steps'][1]['tools']
            self.assertEqual(tools[0]['arguments']['data'],
                '${steps.step_0.tools.step_0_tool_1.outputs.data}')

    def test_missing_output_reports_actual_runtime_contract(self):
        registry = deepcopy(compiler._load_tool_registry_document())
        registry['tools']['transform_data']['returns']['outputs'].pop('data')
        with patch.object(compiler, '_load_tool_registry_document', return_value=registry):
            with self.assertRaisesRegex(ValueError, "producer='transform_data'.*registered_outputs="):
                compiler.compile_workflow(quality_plan(), workflow_id=str(uuid4()))
