from __future__ import annotations

import asyncio
import unittest
import tempfile
from pathlib import Path

from agent_config import load_agent_settings
from app.graphs.nodes.generate_workflow import make_add_current_workflow_candidate
from app.services.workflow_persistence import (
    effective_workflow_snapshot,
    reusable_workflow_snapshot,
)
from app.graphs.nodes.generate_report import (
    _compact_report_payload,
    _build_step_results_from_history,
    build_report_artifact_request,
    execution_is_reportable,
)



class WorkflowPersistenceTests(unittest.TestCase):
    def test_report_payload_truncates_large_text_and_omits_binary_images(self):
        large_text = "A" * 20_000
        compact = _compact_report_payload(
            {
                "status": "SUCCEEDED",
                "outputs": [
                    {"output_type": "stream", "text": large_text},
                    {
                        "output_type": "display_data",
                        "data": {
                            "image/png": "base64-data" * 10_000,
                            "text/plain": large_text,
                        },
                    },
                ],
            }
        )

        self.assertEqual(compact["status"], "SUCCEEDED")
        self.assertLess(len(compact["outputs"][0]["text"]), len(large_text))
        self.assertIn("리포트 입력에서", compact["outputs"][0]["text"])
        self.assertEqual(
            compact["outputs"][1]["data"]["image/png"],
            "<binary image omitted>",
        )
        self.assertLess(
            len(compact["outputs"][1]["data"]["text/plain"]),
            len(large_text),
        )

    def test_reusable_snapshot_clears_execution_bound_inputs(self):
        workflow = {
            "schema_version": "1.3",
            "workflow": {
                "id": "wf-1",
                "status": "ready",
                "context": {"output_dir": "/tmp/run", "locale": "ko"},
                "input_schema": {
                    "dataset_uri": {
                        "type": "str",
                        "required": True,
                        "allow_llm_inference": False,
                        "validation": [],
                    },
                    "top_n": {
                        "type": "int",
                        "required": False,
                        "allow_llm_inference": False,
                        "validation": [],
                    },
                },
                "inputs": {"dataset_uri": "/pv/a.parquet", "top_n": 10},
                "input_provenance": {
                    "dataset_uri": {
                        "source": "confirmed_metadata",
                        "confirmed": True,
                    }
                },
                "unresolved_inputs": [],
                "steps": [],
            },
        }

        snapshot = reusable_workflow_snapshot(workflow)

        definition = snapshot["workflow"]
        self.assertEqual(definition["status"], "needs_input")
        self.assertEqual(definition["inputs"], {})
        self.assertEqual(definition["input_provenance"], {})
        self.assertNotIn("output_dir", definition["context"])
        self.assertEqual(definition["context"]["locale"], "ko")
        self.assertEqual(
            [item["name"] for item in definition["unresolved_inputs"]],
            ["dataset_uri"],
        )

    def test_effective_snapshot_adds_runtime_decision_only(self):
        workflow = {
            "workflow": {
                "id": "wf-1",
                "steps": [
                    {
                        "id": "eda",
                        "tools": [
                            {
                                "id": "eda.boxplot",
                                "execution": "conditional",
                            }
                        ],
                    }
                ],
            }
        }

        snapshot = effective_workflow_snapshot(
            workflow, {"eda.boxplot": "include"}
        )

        tool = snapshot["workflow"]["steps"][0]["tools"][0]
        self.assertEqual(tool["execution"], "conditional")
        self.assertEqual(tool["runtime_decision"], "include")
        self.assertNotIn(
            "runtime_decision", workflow["workflow"]["steps"][0]["tools"][0]
        )

    def test_candidate_has_no_intermediate_execution_plan_reference(self):
        node = make_add_current_workflow_candidate()
        state = {
            "session_id": "session-1",
            "task_id": "task-1",
            "analysis_intent": {"intent": "failure_prediction"},
            "workflow_revision": 2,
            "workflow_id": "wf-1",
            "workflow_origin": "generated",
            "workflow_candidates": [],
            "workflow": {
                "workflow": {
                    "id": "wf-1",
                    "status": "needs_input",
                }
            },
        }

        update = asyncio.run(node(state))

        candidate = update["workflow_candidates"][0]
        self.assertNotIn("execution_plan_id", candidate)
        self.assertEqual(candidate["workflow_id"], "wf-1")
        self.assertEqual(candidate["revision"], 2)

    def test_report_uses_executed_cell_result_pairs(self):
        results = _build_step_results_from_history(
            task_id="task-1",
            execution_id="execution-1",
            execution_steps=[
                {
                    "sequence": 3,
                    "payload": {"source": {"path": "/pv/cell-3.py"}},
                    "lineage": {
                        "skill_name": "eda",
                        "tool_name": "boxplot_eda",
                        "input_parameters": {
                            "step_id": "eda",
                            "tool_id": "eda.boxplot",
                        },
                    },
                }
            ],
            result_history=[
                {
                    "tool_id": "eda.boxplot",
                    "result": {
                        "status": "SUCCEEDED",
                        "cell_index": 3,
                        "outputs": [{"type": "text", "text": "done"}],
                    },
                }
            ],
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].sequence, 3)
        self.assertEqual(results[0].tool_id, "eda.boxplot")
        self.assertEqual(results[0].code_path, "/pv/cell-3.py")
        self.assertEqual(results[0].raw_payload["outputs"][0]["text"], "done")

    def test_report_requires_successful_latest_results_and_no_failures(self):
        state = {
            "execution_status": "SUCCEEDED",
            "executor_result_history": [
                {
                    "tool_id": "tool-a",
                    "result": {"status": "NOT_EXECUTED"},
                },
                {"tool_id": "tool-a", "result": {"status": "SUCCEEDED"}},
                {"tool_id": "tool-b", "result": {"status": "SUCCEEDED"}},
            ],
        }
        self.assertTrue(execution_is_reportable(state))
        state["executor_result_history"].append(
            {"tool_id": "tool-c", "result": {"status": "FAILED"}}
        )
        self.assertFalse(execution_is_reportable(state))

    def test_inline_report_artifact_request(self):
        settings = load_agent_settings(
            {
                "LLM_MODEL_NAME": "test",
                "EXECUTOR_REPORT_SOURCE_TYPE": "INLINE",
            }
        )
        payload, staged_path = build_report_artifact_request(
            settings,
            execution_id="execution-1",
            task_id="task-1",
            workflow_id="workflow-1",
            content="# 분석 결과",
        )

        self.assertIsNone(staged_path)
        self.assertEqual(payload["source"]["type"], "INLINE")
        self.assertEqual(payload["source"]["content"], "# 분석 결과")
        self.assertTrue(payload["append_to_notebook"])

    def test_path_report_artifact_is_staged_under_shared_pv(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            settings = load_agent_settings(
                {
                    "LLM_MODEL_NAME": "test",
                    "EXECUTOR_REPORT_SOURCE_TYPE": "PATH",
                    "EXECUTOR_SHARED_INPUT_ROOT": temporary_root,
                }
            )
            payload, staged_path = build_report_artifact_request(
                settings,
                execution_id="execution-1",
                task_id="task-1",
                workflow_id="workflow-1",
                content="# 분석 결과",
            )

            self.assertEqual(payload["source"]["type"], "PATH")
            self.assertFalse(Path(payload["source"]["path"]).is_absolute())
            self.assertEqual(len(payload["source"]["sha256"]), 64)
            self.assertEqual(Path(staged_path).read_text(), "# 분석 결과")


if __name__ == "__main__":
    unittest.main()
