"""Tests for Redis-resumed Executor result collection."""

from __future__ import annotations

import hashlib
import json
import tempfile
import asyncio
import unittest
from pathlib import Path

from agent_config import load_agent_settings
from app.graphs.nodes.execution_results import make_collect_execution_results
from app.services.execution_notebook_reader import (
    read_current_operation_results,
    read_current_operation_tool_results,
)
from app.services.execution_manifest_reader import _safe_resolve
from app.graphs.routers.orchestration_router import route_after_adaptive_results


def _settings(**overrides):
    environ = {
        "LLM_MODEL_NAME": "test-model",
    }
    environ.update(overrides)
    return load_agent_settings(environ)


class ExecutionResultTests(unittest.TestCase):
    def test_result_read_mode_defaults_to_api_and_rejects_unknown_mode(self):
        self.assertEqual(_settings().executor_result_read_mode, "API")
        with self.assertRaisesRegex(ValueError, "EXECUTOR_RESULT_READ_MODE"):
            _settings(EXECUTOR_RESULT_READ_MODE="UNKNOWN")

    def test_manifest_path_must_stay_under_shared_result_root(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            with self.assertRaisesRegex(ValueError, "unsafe shared-PV"):
                _safe_resolve(root, "../outside/manifest.json")
            with self.assertRaisesRegex(ValueError, "unsafe shared-PV"):
                _safe_resolve(root, "/absolute/manifest.json")

    def test_manifest_mode_reads_event_referenced_step_output_without_api_get(self):
        execution_id = "10000000-0000-0000-0000-000000000001"
        operation_id = "20000000-0000-0000-0000-000000000002"
        step_id = "30000000-0000-0000-0000-000000000003"
        attempt_id = "40000000-0000-0000-0000-000000000004"
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            manifest_dir = root / "executions" / execution_id / "step-result"
            output_path = manifest_dir / "outputs" / "000000-stream-00.txt"
            output_path.parent.mkdir(parents=True)
            output_bytes = b"missing_rate=0.1\n"
            output_path.write_bytes(output_bytes)
            manifest = {
                "schema_version": "1.0",
                "state": "FINALIZED",
                "complete": True,
                "identity": {
                    "execution_id": execution_id,
                    "operation_id": operation_id,
                    "step_id": step_id,
                    "sequence": 0,
                    "execution_attempt_id": attempt_id,
                    "fencing_token": 1,
                },
                "source": {
                    "relative_path": "sources/source.py",
                    "checksum_sha256": "a" * 64,
                    "size_bytes": 1,
                },
                "outputs": [
                    {
                        "ordinal": 0,
                        "kind": "STREAM",
                        "stream_name": "stdout",
                        "execution_count": None,
                        "representations": [
                            {
                                "media_type": "text/plain",
                                "encoding": "UTF8",
                                "relative_path": str(output_path.relative_to(root)),
                                "size_bytes": len(output_bytes),
                                "checksum_sha256": hashlib.sha256(output_bytes).hexdigest(),
                                "complete": True,
                                "truncated_in_preview": False,
                                "metadata": {},
                            }
                        ],
                        "metadata": {},
                        "created_at": "2026-08-26T03:00:00+00:00",
                    }
                ],
                "output_count": 1,
                "representation_count": 1,
                "total_size_bytes": len(output_bytes),
                "execution_count": 1,
                "error_message": None,
                "output_summary": {
                    "output_count": 1,
                    "output_types": {"stream": 1},
                    "stream_names": ["stdout"],
                    "mime_types": ["text/plain"],
                    "has_image": False,
                    "image_count": 0,
                    "has_error": False,
                },
                "created_at": "2026-08-26T03:00:00+00:00",
                "updated_at": "2026-08-26T03:00:01+00:00",
                "completed_at": "2026-08-26T03:00:01+00:00",
            }
            manifest_bytes = json.dumps(manifest).encode()
            manifest_path = manifest_dir / "manifest.json"
            manifest_path.write_bytes(manifest_bytes)
            result_ref = {
                "storage": "SHARED_PV",
                "relative_path": str(manifest_path.relative_to(root)),
                "media_type": "application/json",
                "size_bytes": len(manifest_bytes),
                "checksum_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            }
            state = {
                "execution_id": execution_id,
                "executor_operation_number": 1,
                "execution_event": {
                    "response": {
                        "operation": {"id": operation_id, "number": 1},
                        "step_results": [
                            {
                                "step_id": step_id,
                                "sequence": 0,
                                "status": "SUCCEEDED",
                                "attempt": {"id": attempt_id, "number": 1},
                                "result_ref": result_ref,
                            }
                        ],
                    }
                },
                "execution_steps": [
                    {
                        "sequence": 0,
                        "lineage": {
                            "skill_name": "quality",
                            "tool_name": "profile_data",
                            "input_parameters": {
                                "step_id": "quality",
                                "tool_id": "quality.profile_data",
                            },
                        },
                    }
                ],
            }

            def fail_api(*_args, **_kwargs):
                self.fail("MANIFEST mode must not call an Executor GET API")

            results = asyncio.run(read_current_operation_results(
                _settings(
                    EXECUTOR_RESULT_READ_MODE="MANIFEST",
                    EXECUTOR_SHARED_RESULT_ROOT=temporary_root,
                ),
                state,
                fetch_result=fail_api,
                fetch_notebook=fail_api,
            ))

            # SINGLE receives execution.completed without operation/step_results.
            # Result API provides the index; raw output still comes from the PV.
            api_ref = {k: v for k, v in result_ref.items() if k != "size_bytes"}
            api_ref.update({"attempt_id": attempt_id, "fencing_token": 1})
            terminal = {**state, "executor_wait_phase": "execution_completed",
                        "execution_event": {"event_type": "execution.completed", "status": "SUCCEEDED", "response": {"status": "SUCCEEDED"}}}
            def fetch_terminal_result(*args):
                return {"body": {"operations": [{"operation_id": operation_id, "operation_number": 1,
                    "steps": [{"step_id": step_id, "sequence": 0,
                               "result": {"status": "SUCCEEDED", "result_ref": api_ref}}]}]}}
            terminal_results = asyncio.run(read_current_operation_results(
                _settings(EXECUTOR_RESULT_READ_MODE="MANIFEST", EXECUTOR_SHARED_RESULT_ROOT=temporary_root),
                terminal, fetch_result=fetch_terminal_result, fetch_notebook=fail_api,
            ))
            self.assertEqual(terminal_results[0]["result"]["outputs"], results[0]["result"]["outputs"])
            api_ref["checksum_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                asyncio.run(read_current_operation_results(
                    _settings(EXECUTOR_RESULT_READ_MODE="MANIFEST", EXECUTOR_SHARED_RESULT_ROOT=temporary_root),
                    terminal, fetch_result=fetch_terminal_result, fetch_notebook=fail_api,
                ))

        self.assertEqual(results[0]["tool_id"], "quality.profile_data")
        self.assertEqual(results[0]["result"]["status"], "SUCCEEDED")
        self.assertEqual(
            results[0]["result"]["outputs"][0]["text"],
            "missing_rate=0.1\n",
        )

    def test_failed_step_cancels_instead_of_retrying_skipped_dependents(self):
        state = {
            "execution_id": "execution-1",
            "executor_operation_number": 2,
        }

        def fetch_result(_settings, _execution_id):
            return {
                "body": {
                    "operations": [
                        {
                            "operation_number": 2,
                            "steps": [
                                {
                                    "step_id": "step-15",
                                    "sequence": 15,
                                    "lineage": {
                                        "skill_name": "cleaning",
                                        "tool_name": "select_features",
                                        "input_parameters": {
                                            "step_id": "cleaning",
                                            "tool_id": "cleaning.select_features",
                                        },
                                    },
                                    "result": {
                                        "status": "FAILED",
                                        "error_message": "target column is missing",
                                        "output_summary": {"has_error": True},
                                        "result_ref": {"relative_path": "failed/manifest.json"},
                                    },
                                },
                                {
                                    "step_id": "step-16",
                                    "sequence": 16,
                                    "lineage": {
                                        "skill_name": "modeling",
                                        "tool_name": "split_dataset",
                                        "input_parameters": {
                                            "step_id": "modeling",
                                            "tool_id": "modeling.split_dataset",
                                        },
                                    },
                                    "result": {"status": "SKIPPED"},
                                },
                                {
                                    "step_id": "step-17",
                                    "sequence": 17,
                                    "lineage": {
                                        "skill_name": "workflow",
                                        "tool_name": "workflow_outputs",
                                        "input_parameters": {
                                            "step_id": "workflow-outputs",
                                            "tool_id": "workflow-outputs",
                                        },
                                    },
                                    "result": {"status": "SKIPPED"},
                                },
                            ],
                        }
                    ]
                }
            }

        def fetch_notebook(_settings, _execution_id, **_kwargs):
            return {
                "body": {
                    "cells": [
                        {"index": 15, "execution_count": 16, "outputs": []},
                        {"index": 16, "execution_count": None, "outputs": []},
                        {"index": 17, "execution_count": None, "outputs": []},
                    ]
                }
            }

        results = asyncio.run(read_current_operation_results(
            _settings(),
            state,
            fetch_result=fetch_result,
            fetch_notebook=fetch_notebook,
        ))

        self.assertEqual(results[0]["result"]["status"], "FAILED")
        self.assertEqual(
            results[0]["result"]["error_message"],
            "target column is missing",
        )
        self.assertEqual(results[1]["result"]["status"], "NOT_EXECUTED")
        self.assertEqual(results[2]["role"], "workflow_outputs")
        self.assertEqual(
            route_after_adaptive_results(
                {
                    "execution_status": "FAILED",
                    "executor_tool_results": results,
                }
            ),
            "skip_execution_report",
        )

    def test_terminal_failed_segment_ends_without_cancel_or_finalize(self):
        self.assertEqual(
            route_after_adaptive_results(
                {
                    "execution_status": "FAILED",
                    "executor_tool_results": [
                        {
                            "tool_id": "tool-1",
                            "result": {"status": "FAILED"},
                        }
                    ],
                }
            ),
            "skip_execution_report",
        )

    def test_notebook_cells_are_mapped_to_lineage_tool_ids(self):
        settings = _settings()
        state = {
            "execution_id": "execution-1",
            "executor_operation_steps": [
                {"sequence": 2, "step_id": "executor-step-2"},
                {"sequence": 3, "step_id": "executor-step-3"},
            ],
            "execution_steps": [
                {
                    "sequence": 2,
                    "lineage": {
                        "skill_name": "data_quality_check",
                        "tool_name": "profile_data",
                        "input_parameters": {
                            "step_id": "quality",
                            "tool_id": "quality.profile_data",
                        },
                    },
                },
                {
                    "sequence": 3,
                    "lineage": {
                        "skill_name": "data_quality_check",
                        "tool_name": "compute_statistics",
                        "input_parameters": {
                            "step_id": "quality",
                            "tool_id": "quality.compute_statistics",
                        },
                    },
                },
            ],
        }

        def fetch_notebook(
            _settings,
            _execution_id,
            *,
            view,
            start_index,
            limit,
        ):
            self.assertEqual(view, "FULL")
            self.assertEqual((start_index, limit), (2, 2))
            return {
                "status_code": 200,
                "body": {
                    "cells": [
                        {
                            "index": 2,
                            "execution_count": 3,
                            "output_summary": {"has_error": False},
                            "outputs": [
                                {
                                    "output_type": "stream",
                                    "name": "stdout",
                                    "text": "missing_rate=0.1\n",
                                }
                            ],
                        },
                        {
                            "index": 3,
                            "execution_count": 4,
                            "output_summary": {"has_error": False},
                            "outputs": [],
                        },
                    ]
                },
            }

        results = asyncio.run(read_current_operation_tool_results(
            settings,
            state,
            fetch_notebook=fetch_notebook,
        ))

        self.assertEqual(
            [item["tool_id"] for item in results],
            ["quality.profile_data", "quality.compute_statistics"],
        )
        self.assertEqual(results[0]["result"]["cell_index"], 2)

    def test_unexecuted_notebook_cell_is_not_reported_as_succeeded(self):
        settings = _settings()
        state = {
            "execution_id": "execution-1",
            "executor_operation_steps": [{"sequence": 0}],
            "execution_steps": [
                {
                    "sequence": 0,
                    "lineage": {
                        "skill_name": "eda_analysis",
                        "tool_name": "compute_statistics",
                        "input_parameters": {
                            "step_id": "eda",
                            "tool_id": "eda.compute_statistics",
                        },
                    },
                }
            ],
        }

        def fetch_notebook(_settings, _execution_id, **_kwargs):
            return {
                "status_code": 200,
                "body": {
                    "cells": [
                        {
                            "index": 0,
                            "execution_count": None,
                            "output_summary": {"has_error": False},
                            "outputs": [],
                        }
                    ]
                },
            }

        results = asyncio.run(read_current_operation_tool_results(
            settings,
            state,
            fetch_notebook=fetch_notebook,
        ))

        self.assertEqual(results[0]["result"]["status"], "NOT_EXECUTED")

    def test_redis_event_results_feed_adaptive_observations(self):
        tool_results = [
            {
                "tool_id": "quality.profile_data",
                "result": {"outputs": [{"text": "missing_rate=0.1"}]},
            }
        ]
        node = make_collect_execution_results(
            _settings(),
            adaptive=True,
            read_results=lambda _settings, _state: tool_results,
        )
        update = asyncio.run(node(
            {
                "execution_id": "execution-1",
                "execution_event": {
                    "status": "WAITING_FOR_OPERATION",
                    "state_version": 9,
                },
                "adaptive_observations": {},
                "adaptive_executed_tool_ids": [],
                "adaptive_generated_tool_ids": ["quality.profile_data"],
            }
        ))

        self.assertEqual(update["executor_state_version"], 9)
        self.assertEqual(
            update["adaptive_observations"]["quality.profile_data"],
            tool_results[0]["result"],
        )
        self.assertNotIn("messages", update)
        self.assertEqual(update["executor_tool_results"], tool_results)


if __name__ == "__main__":
    unittest.main()
