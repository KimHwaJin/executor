"""Tests for deterministic compact Plan to Workflow 1.3 compilation."""

from __future__ import annotations

import unittest

from app.agents.workflow_generator.workflow_compiler import compile_workflow_plan
from app.agents.workflow_generator.data_load_steps import (
    merge_required_data_load_steps,
    required_data_load_steps,
    validate_target_data_role_lineage,
)
from app.services.rule_based_notebook_generator import (
    _result_print_lines,
    build_notebook,
)


def _data_argument(step_id: str = "load_data_1", output: str = "data") -> dict:
    return {
        "source": "step_output",
        "step_id": step_id,
        "output": output,
    }


def _tool(name: str, reason: str = "required for analysis") -> dict:
    return {
        "tool": name,
        "selection_reason": reason,
        "arguments": {"data": _data_argument()},
    }


def _base_workflow(steps: list[dict], outputs: dict) -> dict:
    return {
        "plan_version": "1.0",
        "workflow": {
            "id": "compiled-workflow",
            "name": "Compiled workflow",
            "description": "Compile a semantic test plan.",
            "goal": "Test deterministic Workflow compilation.",
            "status": "ready",
            "input_schema": {},
            "inputs": {},
            "input_provenance": {},
            "unresolved_inputs": [],
            "context": {},
            "steps": steps,
            "outputs": outputs,
        },
    }


class WorkflowCompilerTests(unittest.TestCase):
    def test_large_data_tools_do_not_print_dataframe_results(self):
        for tool in ("extract_data", "transform_nce", "transform_wt"):
            self.assertEqual(_result_print_lines(tool, "result"), [])
        self.assertEqual(
            _result_print_lines("merge_data", "result"),
            ['print(result["merge_summary"])'],
        )

    def test_target_aware_tool_rejects_x_only_data_lineage(self):
        selection = {
            "data_count": 2,
            "datasets": [
                {
                    "role": "x", "data_type": "nce", "lot_cd": "6E2",
                    "process": ["ALL"], "query_mode": "period",
                    "start_dt": "2026-05-01", "end_dt": "2026-05-10",
                    "limit": 100, "transform_op": "pivot",
                },
                {
                    "role": "y", "data_type": "wt_symbol", "lot_cd": "6E2",
                    "process": ["PT1H"], "query_mode": "period",
                    "start_dt": "2026-08-01", "end_dt": "2026-08-01",
                    "limit": 100, "transform_op": "wt_fail_pivot",
                },
            ],
        }
        document = {
            "workflow": {
                "steps": [
                    *required_data_load_steps(selection),
                    {
                        "id": "modeling",
                        "order": 3,
                        "tools": [
                            {
                                "id": "select_features",
                                "order": 1,
                                "tool": "select_features",
                                "arguments": {
                                    "data": "${steps.load_data_1.outputs.data}",
                                    "target_column": "${workflow.inputs.target_column}",
                                },
                                "returns": {
                                    "outputs": {"selected_data": {}}
                                },
                            }
                        ],
                        "outputs": {},
                    },
                ]
            }
        }

        with self.assertRaisesRegex(ValueError, "role='y'"):
            validate_target_data_role_lineage(document, selection)

        document["workflow"]["steps"][2]["tools"][0]["arguments"]["data"] = (
            "${steps.load_data_2.outputs.data}"
        )
        validate_target_data_role_lineage(document, selection)

    def test_compiler_corrects_workflow_input_type_from_registry(self):
        plan = _base_workflow(
            [
                {
                    "id": "quality",
                    "skill": "data_quality_check",
                    "depends_on": [],
                    "tools": [
                        _tool("profile_data"),
                        {
                            **_tool("compute_statistics"),
                            "arguments": {
                                "data": _data_argument(),
                                "columns": {
                                    "source": "workflow_input",
                                    "input_name": "columns",
                                },
                            },
                        },
                        _tool("detect_outliers"),
                    ],
                }
            ],
            {
                "statistics": {
                    "step_id": "quality",
                    "tool": "compute_statistics",
                    "output": "statistics",
                }
            },
        )
        workflow = plan["workflow"]
        workflow["input_schema"] = {
            "columns": {
                "type": "str",
                "required": True,
                "allow_llm_inference": False,
                "validation": [],
            }
        }
        workflow["inputs"] = {"columns": ["max_val"]}
        workflow["input_provenance"] = {
            "columns": {"source": "user_answer", "confirmed": True}
        }

        document = compile_workflow_plan(plan)

        self.assertEqual(
            document["workflow"]["input_schema"]["columns"]["type"],
            "list",
        )

    def test_compiler_rejects_conflicting_registry_types_for_one_workflow_input(self):
        plan = _base_workflow(
            [
                {
                    "id": "quality",
                    "skill": "data_quality_check",
                    "depends_on": [],
                    "tools": [
                        {
                            **_tool("profile_data"),
                            "arguments": {
                                "data": {
                                    "source": "workflow_input",
                                    "input_name": "shared_input",
                                }
                            },
                        },
                        {
                            **_tool("compute_statistics"),
                            "arguments": {
                                "data": _data_argument(),
                                "columns": {
                                    "source": "workflow_input",
                                    "input_name": "shared_input",
                                },
                            },
                        },
                        _tool("detect_outliers"),
                    ],
                }
            ],
            {
                "statistics": {
                    "step_id": "quality",
                    "tool": "compute_statistics",
                    "output": "statistics",
                }
            },
        )
        workflow = plan["workflow"]
        workflow["input_schema"] = {
            "shared_input": {
                "type": "str",
                "required": True,
                "allow_llm_inference": False,
                "validation": [],
            }
        }
        workflow["inputs"] = {"shared_input": ["max_val"]}
        workflow["input_provenance"] = {
            "shared_input": {"source": "user_answer", "confirmed": True}
        }

        with self.assertRaisesRegex(ValueError, "incompatible Registry types"):
            compile_workflow_plan(plan)

    def test_compiler_preserves_nullable_registry_defaults(self):
        plan = _base_workflow(
            [
                {
                    "id": "quality",
                    "skill": "data_quality_check",
                    "depends_on": [],
                    "tools": [
                        _tool("profile_data"),
                        _tool("compute_statistics"),
                        _tool("detect_outliers"),
                    ],
                }
            ],
            {
                "profile": {
                    "step_id": "quality",
                    "tool": "profile_data",
                    "output": "profile",
                }
            },
        )

        document = compile_workflow_plan(plan)
        tools = document["workflow"]["steps"][0]["tools"]

        by_name = {tool["tool"]: tool for tool in tools}
        self.assertIsNone(by_name["compute_statistics"]["arguments"]["columns"])
        self.assertIsNone(by_name["detect_outliers"]["arguments"]["columns"])

    def test_compiler_normalizes_empty_optional_columns_to_none(self):
        plan = _base_workflow(
            [
                {
                    "id": "quality",
                    "skill": "data_quality_check",
                    "depends_on": [],
                    "tools": [
                        _tool("profile_data"),
                        {
                            **_tool("compute_statistics"),
                            "arguments": {
                                "data": _data_argument(),
                                "columns": {"source": "planner", "value": []},
                            },
                        },
                        _tool("detect_outliers"),
                    ],
                }
            ],
            {
                "statistics": {
                    "step_id": "quality",
                    "tool": "compute_statistics",
                    "output": "statistics",
                }
            },
        )

        document = compile_workflow_plan(plan)
        statistics_tool = document["workflow"]["steps"][0]["tools"][1]

        self.assertIsNone(statistics_tool["arguments"]["columns"])

    def test_compiler_restores_registry_type_for_stringified_default(self):
        plan = _base_workflow(
            [
                {
                    "id": "quality",
                    "skill": "data_quality_check",
                    "depends_on": [],
                    "tools": [
                        _tool("profile_data"),
                        _tool("compute_statistics"),
                        _tool("detect_outliers"),
                    ],
                },
                {
                    "id": "failure",
                    "skill": "failure_analysis",
                    "depends_on": ["quality"],
                    "tools": [
                        {
                            "tool": "plot_failure_distribution",
                            "selection_reason": "Plot the requested failure distribution.",
                            "arguments": {
                                "data": _data_argument(),
                                "target_column": {
                                    "source": "planner",
                                    "value": "fail",
                                },
                                "positive_label": {
                                    "source": "planner",
                                    "value": "1",
                                },
                            },
                        },
                    ],
                },
            ],
            {
                "class_ratio": {
                    "step_id": "failure",
                    "tool": "plot_failure_distribution",
                    "output": "class_ratio",
                }
            },
        )

        document = compile_workflow_plan(plan)
        failure_tool = document["workflow"]["steps"][1]["tools"][0]

        self.assertEqual(failure_tool["arguments"]["positive_label"], 1)
        self.assertIsInstance(failure_tool["arguments"]["positive_label"], int)

    def test_generated_tool_cell_omits_large_data_io_return_values(self):
        plan = _base_workflow(
            [
                {
                    "id": "quality",
                    "skill": "data_quality_check",
                    "depends_on": [],
                    "tools": [
                        _tool("profile_data"),
                        _tool("compute_statistics"),
                        _tool("detect_outliers"),
                    ],
                }
            ],
            {
                "profile": {
                    "step_id": "quality",
                    "tool": "profile_data",
                    "output": "profile",
                }
            },
        )

        document = merge_required_data_load_steps(
            compile_workflow_plan(plan),
            {
                "data_count": 1,
                "datasets": [
                    {
                        "role": "x",
                        "data_type": "nce", "lot_cd": "6E2",
                        "process": ["ALL"], "query_mode": "period",
                        "start_dt": "2026-05-01", "end_dt": "2026-05-10",
                        "limit": 100, "transform_op": "pivot",
                    }
                ],
            },
        )
        artifact = build_notebook(document, project_root="src")
        tool_cells = [
            cell for cell in artifact["cells"] if cell["role"] == "tool_execution"
        ]

        for cell in tool_cells:
            result_variable = next(
                line.split(" = ", 1)[0]
                for line in cell["code"].splitlines()
                if f" = {cell['tool']}(" in line
            )
            if cell["tool"] in {"extract_data", "transform_nce", "transform_wt"}:
                self.assertNotIn(f"print({result_variable})", cell["code"])
            else:
                self.assertIn(f"print({result_variable})", cell["code"])

    def test_compiler_hydrates_registry_metadata_and_static_execution(self):
        plan = _base_workflow(
            [
                {
                    "id": "quality",
                    "skill": "data_quality_check",
                    "depends_on": [],
                    "tools": [
                        _tool("profile_data"),
                        _tool("compute_statistics"),
                        _tool("detect_outliers"),
                    ],
                }
            ],
            {
                "profile": {
                    "step_id": "quality",
                    "tool": "profile_data",
                    "output": "profile",
                }
            },
        )

        document = compile_workflow_plan(plan)

        workflow = document["workflow"]
        self.assertEqual(workflow["execution_mode"], "static")
        profile_tool = workflow["steps"][0]["tools"][0]
        self.assertEqual(profile_tool["tool_source"], "app/workflow/tools/eda/profile_data.py")
        self.assertEqual(
            profile_tool["returns"]["outputs"]["profile"]["selector"],
            '["profile"]',
        )
        self.assertEqual(profile_tool["execution"], "always")

    def test_compiler_derives_adaptive_execution_and_condition_dependency(self):
        plan = _base_workflow(
            [
                {
                    "id": "quality",
                    "skill": "data_quality_check",
                    "depends_on": [],
                    "tools": [
                        _tool("profile_data"),
                        _tool("compute_statistics"),
                        _tool("detect_outliers"),
                    ],
                },
                {
                    "id": "cleaning",
                    "skill": "data_cleaning_pipeline",
                    "depends_on": ["quality"],
                    "tools": [
                        {
                            "tool": "impute_missing",
                            "selection_reason": "Impute when profiling finds missing values.",
                            "arguments": {"data": _data_argument()},
                        }
                    ],
                },
            ],
            {
                "cleaned_data": {
                    "step_id": "cleaning",
                    "tool": "impute_missing",
                    "output": "imputed_data",
                }
            },
        )

        document = compile_workflow_plan(plan)

        workflow = document["workflow"]
        self.assertEqual(workflow["execution_mode"], "adaptive")
        self.assertEqual(
            workflow["steps"][1]["tools"][0]["execution"],
            "conditional",
        )

    def test_compiler_rejects_missing_external_condition_tool(self):
        plan = _base_workflow(
            [
                {
                    "id": "cleaning",
                    "skill": "data_cleaning_pipeline",
                    "depends_on": [],
                    "tools": [
                        {
                            "tool": "impute_missing",
                            "selection_reason": "Conditional missing value cleanup.",
                            "arguments": {"data": _data_argument()},
                        }
                    ],
                }
            ],
            {
                "cleaned_data": {
                    "step_id": "cleaning",
                    "tool": "impute_missing",
                    "output": "imputed_data",
                }
            },
        )

        with self.assertRaisesRegex(
            ValueError,
            r"requires condition_tool data_quality_check\.profile_data",
        ):
            compile_workflow_plan(plan)

    def test_compiler_rejects_unknown_data_load_reference(self):
        plan = _base_workflow(
            [
                {
                    "id": "quality",
                    "skill": "data_quality_check",
                    "depends_on": [],
                    "tools": [
                        {
                            **_tool(name),
                            "arguments": {
                                "data": _data_argument(step_id="load_data_0")
                            },
                        }
                        for name in (
                            "profile_data",
                            "compute_statistics",
                            "detect_outliers",
                        )
                    ],
                }
            ],
            {
                "profile": {
                    "step_id": "quality",
                    "tool": "profile_data",
                    "output": "profile",
                }
            },
        )

        with self.assertRaisesRegex(
            ValueError,
            r"unknown Step output: load_data_0\.data",
        ):
            compile_workflow_plan(
                plan,
                external_step_outputs={("load_data_1", "data")},
            )

    def test_compiled_adaptive_workflow_builds_initial_execution_prefix(self):
        plan = _base_workflow(
            [
                {
                    "id": "quality",
                    "skill": "data_quality_check",
                    "depends_on": [],
                    "tools": [
                        _tool("profile_data"),
                        _tool("compute_statistics"),
                        _tool("detect_outliers"),
                    ],
                },
                {
                    "id": "cleaning",
                    "skill": "data_cleaning_pipeline",
                    "depends_on": ["quality"],
                    "tools": [
                        {
                            "tool": "impute_missing",
                            "selection_reason": "Conditional missing value cleanup.",
                            "arguments": {"data": _data_argument()},
                        }
                    ],
                },
            ],
            {
                "cleaned_data": {
                    "step_id": "cleaning",
                    "tool": "impute_missing",
                    "output": "imputed_data",
                }
            },
        )
        document = compile_workflow_plan(plan)
        document = merge_required_data_load_steps(
            document,
            {
                "data_count": 1,
                "datasets": [
                    {
                        "role": "x",
                        "data_type": "nce", "lot_cd": "6E2",
                        "process": ["ALL"], "query_mode": "period",
                        "start_dt": "2026-05-01", "end_dt": "2026-05-10",
                        "limit": 100, "transform_op": "pivot",
                    }
                ],
            },
        )

        artifact = build_notebook(document, project_root="src")

        self.assertEqual(
            [cell["tool"] for cell in artifact["cells"]],
            [
                "extract_data",
                "transform_nce",
                "profile_data",
                "compute_statistics",
                "detect_outliers",
            ],
        )


if __name__ == "__main__":
    unittest.main()
