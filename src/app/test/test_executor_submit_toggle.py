"""Tests for generating Executor requests without HTTP submission."""

from __future__ import annotations

import tempfile
import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import UUID

from agent_config import load_agent_settings, require_executor_tenant
from app.agent_worker.graph_boundary import ExecutorBoundaryNodes
from app.graphs.nodes.executor_request import make_build_executor_request
from app.graphs.nodes.adaptive_executor import (
    make_cancel_adaptive_execution,
    make_finalize_adaptive_execution,
    make_submit_adaptive_operation,
)


class ExecutorSubmitToggleTests(unittest.TestCase):
    def test_failed_adaptive_execution_is_cancelled_with_error_reason(self):
        calls = []

        def submit_cancel(_settings, execution_id, payload):
            calls.append((execution_id, payload))
            return {"status_code": 202, "body": {"state": {"status": "CANCEL_REQUESTED"}}}

        settings = load_agent_settings(
            {
                "LLM_MODEL_NAME": "test-model",
                "EXECUTOR_SUBMIT_ENABLED": "true",
                "DEMO_ARTIFACTS_ENABLED": "false",
            }
        )
        update = asyncio.run(make_cancel_adaptive_execution(
            settings,
            submit_cancel=submit_cancel,
        )(
            {
                "execution_id": "execution-1",
                "task_id": "task-1",
                "executor_tool_results": [
                    {
                        "tool_id": "cleaning.select_features",
                        "result": {
                            "status": "FAILED",
                            "error_message": "target column is missing",
                        },
                    }
                ],
            }
        ))

        self.assertEqual(calls[0][0], "execution-1")
        self.assertEqual(calls[0][1]["reason"], "target column is missing")
        self.assertEqual(calls[0][1]["actor"], {"type": "AGENT", "id": "task-1"})
        self.assertEqual(update["executor_wait_phase"], "execution_completed")
        self.assertEqual(update["execution_status"], "CANCEL_REQUESTED")

    def test_disabled_submit_writes_request_without_calling_executor(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            cell_path = Path(temporary_root) / "001_test.py"
            cell_path.write_text("result = 1\n", encoding="utf-8")
            settings = load_agent_settings(
                {
                    "LLM_MODEL_NAME": "test-model",
                    "EXECUTOR_SUBMIT_ENABLED": "false",
                    "DEMO_ARTIFACTS_ENABLED": "true",
                    "DEMO_ARTIFACTS_ROOT": temporary_root,
                    "EXECUTOR_SHARED_INPUT_ROOT": temporary_root,
                }
            )

            def unexpected_submit(*_args, **_kwargs):
                raise AssertionError("Executor submitter must not be called")

            node = make_build_executor_request(
                settings,
                submit_start=unexpected_submit,
            )
            result = asyncio.run(node(
                {
                    "user_id": "user",
                    "project_id": "project",
                    "session_id": "session",
                    "task_id": "task",
                    "workflow": {
                        "schema_version": "1.3",
                        "workflow": {
                            "id": "workflow",
                            "execution_mode": "static",
                        },
                    },
                    "notebook": {
                        "cells": [
                            {
                                "id": "step-tool",
                                "role": "tool_execution",
                                "skill": "skill",
                                "step_id": "step",
                                "tool_id": "tool-id",
                                "tool": "tool",
                                "code": "result = 1\n",
                            }
                        ]
                    },
                    "artifact_files": {"cells": [str(cell_path)]},
                }
            ))

            self.assertEqual(result["execution_status"], "not_submitted")
            self.assertEqual(
                result["final_response"]["status"],
                "executor_request_created",
            )
            self.assertTrue(result["executor_submit_response"]["skipped"])
            self.assertTrue(Path(result["executor_request_path"]).is_file())
            step = result["execution_steps"][0]
            self.assertEqual(step["payload"]["type"], "PYTHON_EXECUTE")
            self.assertEqual(step["payload"]["source"]["type"], "PATH")
            self.assertEqual(len(step["payload"]["source"]["sha256"]), 64)
            source_path = Path(step["payload"]["source"]["path"])
            self.assertFalse(source_path.is_absolute())
            self.assertEqual(source_path.parts[0], "d-test")
            staged_path = settings.executor_shared_input_root / source_path
            self.assertEqual(staged_path.read_text(encoding="utf-8"), "result = 1\n")

    def test_inline_source_embeds_notebook_cell_code(self):
        settings = load_agent_settings(
            {
                "LLM_MODEL_NAME": "test-model",
                "EXECUTOR_SUBMIT_ENABLED": "false",
                "EXECUTOR_SOURCE_TYPE": "inline",
                "DEMO_ARTIFACTS_ENABLED": "false",
            }
        )
        node = make_build_executor_request(settings)
        result = asyncio.run(node(
            {
                "user_id": "user",
                "project_id": "project",
                "session_id": "session",
                "task_id": "task",
                "workflow": {
                    "schema_version": "1.3",
                    "workflow": {
                        "id": "workflow",
                        "execution_mode": "static",
                    },
                },
                "notebook": {
                    "cells": [
                        {
                            "id": "step-tool",
                            "role": "tool_execution",
                            "skill": "skill",
                            "step_id": "step",
                            "tool_id": "tool-id",
                            "tool": "tool",
                            "code": "result = 1\nprint(result)\n",
                        }
                    ]
                },
                "artifact_files": {},
            }
        ))

        source = result["execution_steps"][0]["payload"]["source"]
        self.assertEqual(
            source,
            {
                "type": "INLINE",
                "content": "result = 1\nprint(result)\n",
            },
        )

    def test_tenant_id_is_sent_only_when_configured(self):
        state = {
            "user_id": "user",
            "project_id": "project",
            "session_id": "session",
            "task_id": "task",
            "workflow": {
                "schema_version": "1.3",
                "workflow": {"id": "workflow", "execution_mode": "static"},
            },
            "notebook": {
                "cells": [
                    {
                        "id": "step-tool",
                        "role": "tool_execution",
                        "skill": "skill",
                        "step_id": "step",
                        "tool_id": "tool-id",
                        "tool": "tool",
                        "code": "result = 1\n",
                    }
                ]
            },
            "artifact_files": {},
        }
        for tenant in ("dtest-agent", None):
            with self.subTest(tenant=tenant), tempfile.TemporaryDirectory() as root:
                environ = {
                    "MODEL_NAME": "test-model",
                    "EXECUTOR_SUBMIT_ENABLED": "false",
                    "EXECUTOR_SOURCE_TYPE": "inline",
                    "DEMO_ARTIFACTS_ENABLED": "true",
                    "DEMO_ARTIFACTS_ROOT": root,
                }
                if tenant:
                    environ["EXECUTOR_TENANT_ID"] = tenant
                settings = load_agent_settings(environ)
                result = asyncio.run(make_build_executor_request(settings)(dict(state)))
                payload = json.loads(
                    Path(result["executor_request_path"]).read_text(encoding="utf-8")
                )
                self.assertEqual(payload.get("tenant_id"), tenant)
                self.assertEqual("tenant_id" in payload, tenant is not None)

    def test_tenant_is_validated_and_required_at_startup(self):
        for invalid in ("work", "dlq", "Dtest", "-dtest"):
            with self.subTest(tenant=invalid), self.assertRaisesRegex(
                ValueError, "EXECUTOR_TENANT_ID"
            ):
                load_agent_settings({"MODEL_NAME": "m", "EXECUTOR_TENANT_ID": invalid})
        with self.assertRaisesRegex(RuntimeError, "EXECUTOR_TENANT_ID is required"):
            require_executor_tenant(load_agent_settings({"MODEL_NAME": "m"}))
        self.assertEqual(
            require_executor_tenant(
                load_agent_settings({"MODEL_NAME": "m", "EXECUTOR_TENANT_ID": "dtest"})
            ),
            "dtest",
        )

    def test_registration_carries_the_submit_tenant(self):
        bindings = Mock(register=AsyncMock())
        nodes = ExecutorBoundaryNodes(bindings, "dtest-agent")
        execution_id = "7eab4cd7-e124-4ee2-a888-cfc69f7cb298"
        asyncio.run(
            nodes.register_execution(
                {"task_id": "task", "execution_id": execution_id},
                {"configurable": {"thread_id": "session"}},
            )
        )
        bindings.register.assert_awaited_once_with(
            execution_id=UUID(execution_id),
            session_id="session",
            task_id="task",
            tenant="dtest-agent",
        )

    def test_adaptive_initial_request_contains_entire_generated_prefix(self):
        calls = []

        def submit(_settings, payload):
            calls.append(payload)
            steps = payload["operation"]["spec"]["steps"]
            return {
                "status_code": 202,
                "body": {
                    "execution_id": "execution-1",
                    "operation": {
                        "operation_id": "operation-1",
                        "steps": [
                            {"sequence": step["sequence"], "step_id": f"step-{index}"}
                            for index, step in enumerate(steps)
                        ],
                    },
                    "state": {"status": "QUEUED", "version": 1},
                },
            }

        settings = load_agent_settings(
            {
                "LLM_MODEL_NAME": "test-model",
                "EXECUTOR_SUBMIT_ENABLED": "true",
                "EXECUTOR_SOURCE_TYPE": "INLINE",
                "DEMO_ARTIFACTS_ENABLED": "false",
            }
        )
        cells = [
            {
                "id": f"step-{index}",
                "role": "tool_execution",
                "skill": "quality",
                "step_id": "quality",
                "tool_id": f"tool-{index}",
                "tool": f"tool_{index}",
                "code": f"result_{index} = {index}\n",
            }
            for index in range(3)
        ]
        result = asyncio.run(make_build_executor_request(settings, submit_start=submit)(
            {
                "user_id": "user",
                "project_id": "project",
                "session_id": "session",
                "task_id": "task",
                "workflow": {
                    "workflow": {"id": "workflow", "execution_mode": "adaptive"}
                },
                "notebook": {"cells": cells},
                "artifact_files": {},
            }
        ))

        self.assertEqual(calls[0]["lifecycle"]["operation_mode"], "MULTI")
        self.assertEqual(len(calls[0]["operation"]["spec"]["steps"]), 3)
        self.assertEqual(result["executor_next_sequence"], 3)
        self.assertEqual(result["executor_state_version"], 1)

    def test_adaptive_followup_and_finalize_use_latest_version(self):
        operation_calls = []
        finalize_calls = []

        def submit_continue(_settings, execution_id, payload):
            operation_calls.append((execution_id, payload))
            return {
                "status_code": 202,
                "body": {
                    "execution_id": execution_id,
                    "operation": {
                        "operation_id": "operation-2",
                        "steps": [{"sequence": 3, "step_id": "executor-step-3"}],
                    },
                    "state": {"status": "QUEUED", "version": 5},
                },
            }

        def submit_finish(_settings, execution_id, payload):
            finalize_calls.append((execution_id, payload))
            return {
                "status_code": 202,
                "body": {
                    "execution_id": execution_id,
                    "operation": None,
                    "state": {"status": "FINALIZING", "version": 7},
                },
            }

        settings = load_agent_settings(
            {
                "LLM_MODEL_NAME": "test-model",
                "EXECUTOR_SUBMIT_ENABLED": "true",
                "EXECUTOR_SOURCE_TYPE": "INLINE",
                "DEMO_ARTIFACTS_ENABLED": "false",
            }
        )
        state = {
            "user_id": "user",
            "project_id": "project",
            "session_id": "session",
            "task_id": "task",
            "execution_id": "execution-1",
            "executor_state_version": 4,
            "executor_next_sequence": 3,
            "executor_operation_number": 1,
            "execution_steps": [],
            "adaptive_round": 2,
            "notebook": {
                "cells": [{
                    "id": "clean-impute",
                    "role": "tool_execution",
                    "skill": "cleaning",
                    "step_id": "clean",
                    "tool_id": "impute",
                    "tool": "impute_missing",
                    "code": "cleaned = data\n",
                }]
            },
            "artifact_files": {},
        }
        update = asyncio.run(make_submit_adaptive_operation(
            settings,
            submit_continue=submit_continue,
        )(state))

        self.assertEqual(operation_calls[0][1]["expected_version"], 4)
        self.assertEqual(operation_calls[0][1]["spec"]["steps"][0]["sequence"], 3)
        self.assertEqual(update["executor_next_sequence"], 4)
        self.assertEqual(update["executor_state_version"], 5)

        finalize_state = {**state, **update, "executor_state_version": 6}
        final = asyncio.run(make_finalize_adaptive_execution(
            settings,
            submit_finish=submit_finish,
        )(finalize_state))
        self.assertEqual(finalize_calls[0][1]["expected_version"], 6)
        self.assertEqual(final["execution_status"], "FINALIZING")


if __name__ == "__main__":
    unittest.main()
