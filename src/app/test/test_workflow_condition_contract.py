"""Tests for Skill condition Tool dependencies in generated Workflows."""

from __future__ import annotations

import unittest

from app.agents.workflow_generator.workflow_generator_tools import (
    canonicalize_registry_tool_sources,
    validate_skill_condition_contract,
)


def _tool(name: str, order: int, *, execution: str = "always") -> dict:
    return {
        "id": name,
        "order": order,
        "tool": name,
        "execution": execution,
        "condition": None,
    }


def _cleaning_step(order: int = 2) -> dict:
    return {
        "id": "cleaning",
        "order": order,
        "skill": "data_cleaning_pipeline",
        "tools": [_tool("impute_missing", 1, execution="conditional")],
    }


def _quality_step(order: int = 1) -> dict:
    return {
        "id": "quality",
        "order": order,
        "skill": "data_quality_check",
        "tools": [
            _tool("profile_data", 1),
            _tool("compute_statistics", 2),
            _tool("detect_outliers", 3),
        ],
    }


class WorkflowConditionContractTests(unittest.TestCase):
    def test_canonicalizes_registry_output_selector(self):
        document = {
            "workflow": {
                "steps": [
                    {
                        "tools": [
                            {
                                "tool": "merge_data",
                                "tool_source": "invented/path.py",
                                "returns": {
                                    "outputs": {
                                        "merged_data": {
                                            "selector": "merged_data",
                                            "variable": "merged_data",
                                        }
                                    }
                                },
                            }
                        ]
                    }
                ]
            }
        }

        canonicalize_registry_tool_sources(document)

        workflow_tool = document["workflow"]["steps"][0]["tools"][0]
        self.assertEqual(
            workflow_tool["tool_source"],
            "app/workflow/tools/preprocessing/merge_data.py",
        )
        self.assertEqual(
            workflow_tool["returns"]["outputs"]["merged_data"]["selector"],
            '["merged_data"]',
        )

    def test_canonicalizes_single_registry_return_output_and_references(self):
        document = {
            "workflow": {
                "steps": [
                    {
                        "id": "profile",
                        "tools": [
                            {
                                "id": "profile_tool",
                                "tool": "profile_data",
                                "returns": {
                                    "outputs": {
                                        "profile_report": {
                                            "selector": "profile_report",
                                            "variable": "profile_report",
                                        }
                                    }
                                },
                            }
                        ],
                        "outputs": {
                            "profile_report": (
                                "${steps.profile.tools.profile_tool.outputs."
                                "profile_report}"
                            )
                        },
                    }
                ]
            }
        }

        canonicalize_registry_tool_sources(document)

        workflow_step = document["workflow"]["steps"][0]
        outputs = workflow_step["tools"][0]["returns"]["outputs"]
        self.assertEqual(list(outputs), ["profile"])
        self.assertEqual(outputs["profile"]["selector"], '["profile"]')
        self.assertEqual(
            workflow_step["outputs"]["profile_report"],
            "${steps.profile.tools.profile_tool.outputs.profile}",
        )

    def test_rejects_ambiguous_return_output_missing_from_registry(self):
        document = {
            "workflow": {
                "steps": [
                    {
                        "id": "merge",
                        "tools": [
                            {
                                "id": "merge_tool",
                                "tool": "merge_data",
                                "returns": {
                                    "outputs": {
                                        "merged": {
                                            "selector": "merged",
                                            "variable": "merged",
                                        }
                                    }
                                },
                            }
                        ],
                    }
                ]
            }
        }

        with self.assertRaisesRegex(ValueError, r"merge_data\.merged"):
            canonicalize_registry_tool_sources(document)

    def test_rejects_missing_external_condition_tool(self):
        document = {
            "workflow": {
                "execution_mode": "adaptive",
                "steps": [_cleaning_step()],
            }
        }

        with self.assertRaisesRegex(
            ValueError,
            r"requires condition_tool data_quality_check\.profile_data",
        ):
            validate_skill_condition_contract(document)

    def test_rejects_external_condition_tool_after_candidate(self):
        document = {
            "workflow": {
                "execution_mode": "adaptive",
                "steps": [_cleaning_step(order=1), _quality_step(order=2)],
            }
        }

        with self.assertRaisesRegex(
            ValueError,
            r"data_quality_check\.profile_data must appear before",
        ):
            validate_skill_condition_contract(document)

    def test_accepts_external_condition_tool_before_candidate(self):
        document = {
            "workflow": {
                "execution_mode": "adaptive",
                "steps": [_quality_step(order=1), _cleaning_step(order=2)],
            }
        }

        validate_skill_condition_contract(document)


if __name__ == "__main__":
    unittest.main()
