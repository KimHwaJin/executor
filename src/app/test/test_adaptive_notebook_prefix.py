"""Tests for initial adaptive notebook code generation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.rule_based_notebook_generator import build_notebook


def _workflow_tool(
    tool_id: str,
    order: int,
    *,
    execution: str,
    data_reference: str | None = None,
) -> dict:
    arguments = {} if data_reference is None else {"data": data_reference}
    argument_sources = (
        {} if data_reference is None else {"data": "step_output"}
    )
    return {
        "id": tool_id,
        "order": order,
        "tool": tool_id,
        "tool_source": f"tools/{tool_id}.py",
        "execution": execution,
        "arguments": arguments,
        "argument_sources": argument_sources,
        "returns": {
            "result_variable": f"{tool_id}_result",
            "outputs": {
                "data": {
                    "selector": '["data"]',
                    "variable": f"{tool_id}_data",
                }
            },
        },
    }


class AdaptiveNotebookPrefixTests(unittest.TestCase):
    def test_builds_only_cells_before_first_unresolved_conditional_tool(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            tools_dir = root / "tools"
            tools_dir.mkdir()
            for tool_name in ("observe", "clean", "analyze"):
                (tools_dir / f"{tool_name}.py").write_text(
                    f"def {tool_name}(data=None):\n"
                    f"    return {{'data': {tool_name!r}}}\n",
                    encoding="utf-8",
                )

            observe = _workflow_tool("observe", 1, execution="always")
            clean = _workflow_tool(
                "clean",
                1,
                execution="conditional",
                data_reference="${steps.observe.outputs.data}",
            )
            analyze = _workflow_tool(
                "analyze",
                1,
                execution="always",
                data_reference="${steps.clean.outputs.data}",
            )
            document = {
                "schema_version": "1.3",
                "workflow": {
                    "id": "adaptive-test",
                    "status": "ready",
                    "execution_mode": "adaptive",
                    "input_schema": {},
                    "inputs": {},
                    "input_provenance": {},
                    "unresolved_inputs": [],
                    "steps": [
                        {
                            "id": "observe",
                            "order": 1,
                            "skill": "quality",
                            "depends_on": [],
                            "execution": "always",
                            "tools": [observe],
                            "outputs": {
                                "data": "${steps.observe.tools.observe.outputs.data}"
                            },
                        },
                        {
                            "id": "clean",
                            "order": 2,
                            "skill": "cleaning",
                            "depends_on": ["observe"],
                            "execution": "always",
                            "tools": [clean],
                            "outputs": {
                                "data": "${steps.clean.tools.clean.outputs.data}"
                            },
                        },
                        {
                            "id": "analyze",
                            "order": 3,
                            "skill": "analysis",
                            "depends_on": ["clean"],
                            "execution": "always",
                            "tools": [analyze],
                            "outputs": {
                                "data": "${steps.analyze.tools.analyze.outputs.data}"
                            },
                        },
                    ],
                    "outputs": {
                        "data": "${steps.analyze.outputs.data}"
                    },
                },
            }

            artifact = build_notebook(document, project_root=root)

        self.assertEqual(
            [cell["id"] for cell in artifact["cells"]],
            ["observe-observe"],
        )
        self.assertNotIn(
            "workflow-outputs",
            [cell["id"] for cell in artifact["cells"]],
        )
        self.assertIn("observe_result = observe(", artifact["cells"][0]["code"])

    def test_runtime_include_builds_remaining_segment(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            tools_dir = root / "tools"
            tools_dir.mkdir()
            for tool_name in ("observe", "clean", "analyze"):
                (tools_dir / f"{tool_name}.py").write_text(
                    f"def {tool_name}(data=None):\n"
                    f"    return {{'data': {tool_name!r}}}\n",
                    encoding="utf-8",
                )
            observe = _workflow_tool("observe", 1, execution="always")
            clean = _workflow_tool(
                "clean",
                1,
                execution="conditional",
                data_reference="${steps.observe.outputs.data}",
            )
            analyze = _workflow_tool(
                "analyze",
                1,
                execution="always",
                data_reference="${steps.clean.outputs.data}",
            )
            document = {
                "schema_version": "1.3",
                "workflow": {
                    "id": "adaptive-test",
                    "status": "ready",
                    "execution_mode": "adaptive",
                    "input_schema": {},
                    "inputs": {},
                    "input_provenance": {},
                    "unresolved_inputs": [],
                    "steps": [
                        {"id": "observe", "order": 1, "skill": "quality", "depends_on": [], "execution": "always", "tools": [observe], "outputs": {"data": "${steps.observe.tools.observe.outputs.data}"}},
                        {"id": "clean", "order": 2, "skill": "cleaning", "depends_on": ["observe"], "execution": "always", "tools": [clean], "outputs": {"data": "${steps.clean.tools.clean.outputs.data}"}},
                        {"id": "analyze", "order": 3, "skill": "analysis", "depends_on": ["clean"], "execution": "always", "tools": [analyze], "outputs": {"data": "${steps.analyze.tools.analyze.outputs.data}"}},
                    ],
                    "outputs": {"data": "${steps.analyze.outputs.data}"},
                },
            }

            artifact = build_notebook(
                document,
                project_root=root,
                executed_tool_ids={"observe"},
                runtime_decisions={"clean": "include"},
            )

        self.assertEqual(
            [cell["tool"] for cell in artifact["cells"]],
            ["clean", "analyze", None],
        )
        self.assertIsNone(
            artifact["workflow"]["pending_conditional_tool_id"]
        )

    def test_runtime_exclude_passes_input_data_to_downstream_tool(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            tools_dir = root / "tools"
            tools_dir.mkdir()
            for tool_name in ("observe", "clean", "analyze"):
                (tools_dir / f"{tool_name}.py").write_text(
                    f"def {tool_name}(data=None):\n"
                    f"    return {{'data': {tool_name!r}}}\n",
                    encoding="utf-8",
                )
            observe = _workflow_tool("observe", 1, execution="always")
            clean = _workflow_tool("clean", 1, execution="conditional", data_reference="${steps.observe.outputs.data}")
            analyze = _workflow_tool("analyze", 1, execution="always", data_reference="${steps.clean.outputs.data}")
            document = {
                "schema_version": "1.3",
                "workflow": {
                    "id": "adaptive-test", "status": "ready", "execution_mode": "adaptive",
                    "input_schema": {}, "inputs": {}, "input_provenance": {}, "unresolved_inputs": [],
                    "steps": [
                        {"id": "observe", "order": 1, "skill": "quality", "depends_on": [], "execution": "always", "tools": [observe], "outputs": {"data": "${steps.observe.tools.observe.outputs.data}"}},
                        {"id": "clean", "order": 2, "skill": "cleaning", "depends_on": ["observe"], "execution": "always", "tools": [clean], "outputs": {"data": "${steps.clean.tools.clean.outputs.data}"}},
                        {"id": "analyze", "order": 3, "skill": "analysis", "depends_on": ["clean"], "execution": "always", "tools": [analyze], "outputs": {"data": "${steps.analyze.tools.analyze.outputs.data}"}},
                    ],
                    "outputs": {"data": "${steps.analyze.outputs.data}"},
                },
            }

            artifact = build_notebook(
                document,
                project_root=root,
                executed_tool_ids={"observe"},
                runtime_decisions={"clean": "exclude"},
            )

        self.assertEqual(
            [cell["tool"] for cell in artifact["cells"]],
            ["analyze", None],
        )
        self.assertIn("data=observe_data", artifact["cells"][0]["code"])


if __name__ == "__main__":
    unittest.main()
