"""Workflow 1.0 fixtures through the existing 1.3 notebook generator."""

import ast
from copy import deepcopy
import inspect
import json
from pathlib import Path
import tempfile
import unittest

from app.agents.workflow_generator.workflow_standard_compiler import (
    compile_workflow, extract_workflow_inputs,
)
from app.schemas.workflows.workflow_format import WorkflowGeneratorOutput
from app.services.rule_based_notebook_generator import APP_SOURCE_ROOT, build_notebook
from app.workflow.tools.eda.compute_statistics import compute_statistics


FIXTURES = Path(__file__).parent / "fixtures" / "workflow_standard"
WORKFLOW_ID = "00000000-0000-0000-0000-000000000001"


def example(mode="static"):
    return json.loads((FIXTURES / f"{mode}.json").read_text(encoding="utf-8"))


def bindings(document):
    values = {
        "lot_cd": "LOT1", "process": ["P01"], "start_dt": "2026-01-01",
        "end_dt": "2026-02-01", "transform_op": "pivot", "sample_size": 0.5,
        "join_columns": ["alias_lot_id", "wf_id", "x", "y"], "columns": ["x"],
    }
    return [
        {"step_index": f["step_index"], "tool_index": f["tool_index"],
         "parameter": f["parameter"], "value": (
             ("nce" if f["tool_index"] == 0 else "wt_symbol")
             if f["parameter"] == "data_type" else values[f["parameter"]]
         )}
        for f in extract_workflow_inputs(document)
    ]


def compile_ready(document):
    return compile_workflow(document, workflow_id=WORKFLOW_ID, execution_inputs=bindings(document))


class StandardCompilerTests(unittest.TestCase):
    def check_cells(self, notebook):
        for cell in notebook["cells"]:
            tree = ast.parse(cell["code"])
            compile(tree, cell["id"], "exec")
            if cell.get("tool"):
                function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == cell["tool"])
                allowed = {a.arg for a in function.args.args + function.args.kwonlyargs}
                calls = [n for n in tree.body if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                         and isinstance(n.value.func, ast.Name) and n.value.func.id == cell["tool"]]
                self.assertTrue(calls)
                self.assertLessEqual({kw.arg for kw in calls[-1].value.keywords}, allowed)

    def test_static_preserves_definition_and_generates_every_call(self):
        document = example()
        original = deepcopy(document)
        compiled = compile_ready(document)
        self.assertEqual(document, original)
        self.assertEqual(compiled, compile_ready(document))
        WorkflowGeneratorOutput.model_validate(compiled)
        self.assertEqual(compiled["workflow"]["id"], WORKFLOW_ID)
        self.assertEqual(len(compiled["workflow"]["steps"]), 3)
        notebook = build_notebook(compiled, project_root=APP_SOURCE_ROOT)
        self.assertEqual(len(notebook["cells"]), 6)
        self.check_cells(notebook)
        tools = compiled["workflow"]["steps"][0]["tools"]
        self.assertEqual(tools[1]["arguments"]["data"], "${steps.step_0.tools.step_0_tool_0.outputs.data}")
        self.assertNotIn("kwargs", tools[0]["arguments"])

    def test_missing_input_is_not_filled_from_default(self):
        document = example()
        compiled = compile_workflow(document, workflow_id=WORKFLOW_ID)
        self.assertEqual(compiled["workflow"]["status"], "needs_input")
        self.assertEqual(len(compiled["workflow"]["unresolved_inputs"]), len(bindings(document)))
        self.assertIsNone(compiled["workflow"]["inputs"]["step_0_tool_1_transform_op"])
        with self.assertRaisesRegex(ValueError, "ready"):
            build_notebook(compiled, project_root=APP_SOURCE_ROOT)

    def test_input_types_and_addresses_are_preserved(self):
        fields = extract_workflow_inputs(example("adaptive"))
        self.assertEqual(next(f["type"] for f in fields if f["parameter"] == "sample_size"), "int | float")
        lots = [f for f in fields if f["parameter"] == "lot_cd"]
        self.assertEqual(len(lots), 2)
        self.assertNotEqual(lots[0]["name"], lots[1]["name"])

    def test_input_origin_confirmation_and_null(self):
        document = example()
        supplied = bindings(document)
        lot = next(b for b in supplied if b["parameter"] == "lot_cd")
        lot.update(value=None, origin="user_request", confirmed=False)
        c = compile_workflow(document, workflow_id=WORKFLOW_ID, execution_inputs=supplied)
        self.assertEqual(c["workflow"]["status"], "needs_input")
        lot["confirmed"] = True
        c = compile_workflow(document, workflow_id=WORKFLOW_ID, execution_inputs=supplied)
        self.assertEqual(c["workflow"]["status"], "ready")
        self.assertEqual(c["workflow"]["input_provenance"]["step_0_tool_0_lot_cd"]["source"], "user_query")
        self.check_cells(build_notebook(c, project_root=APP_SOURCE_ROOT))

    def test_adaptive_prefix_and_all_decision_combinations(self):
        compiled = compile_ready(example("adaptive"))
        prefix = build_notebook(compiled, project_root=APP_SOURCE_ROOT)
        self.assertEqual(len(prefix["cells"]), 7)
        self.assertEqual(prefix["workflow"]["pending_conditional_tool_id"], "step_2_tool_1")
        done = {c["tool_id"] for c in prefix["cells"]}
        for box in ("include", "exclude"):
            for corr in ("include", "exclude"):
                decisions = {"step_2_tool_1": box, "step_2_tool_2": corr}
                result = build_notebook(compiled, project_root=APP_SOURCE_ROOT,
                                        executed_tool_ids=done, runtime_decisions=decisions)
                self.check_cells(result)
                self.assertIsNone(result["workflow"].get("pending_conditional_tool_id"))
                emitted = {c["tool"] for c in result["cells"]}
                self.assertEqual("boxplot_eda" in emitted, box == "include")
                self.assertEqual("correlation_analysis" in emitted, corr == "include")

    def test_type_errors_are_not_coerced(self):
        for invalid in (True, "0.5", float("nan")):
            document = example()
            supplied = bindings(document)
            next(b for b in supplied if b["parameter"] == "sample_size")["value"] = invalid
            with self.assertRaises(ValueError):
                compile_workflow(document, workflow_id=WORKFLOW_ID, execution_inputs=supplied)

    def test_adaptive_advances_one_decision_at_a_time(self):
        compiled = compile_ready(example("adaptive"))
        prefix = build_notebook(compiled, project_root=APP_SOURCE_ROOT)
        done = {c["tool_id"] for c in prefix["cells"]}
        segment = build_notebook(compiled, project_root=APP_SOURCE_ROOT,
                                 executed_tool_ids=done,
                                 runtime_decisions={"step_2_tool_1": "include"})
        self.assertEqual([c["tool"] for c in segment["cells"]], ["boxplot_eda"])
        self.assertEqual(segment["workflow"]["pending_conditional_tool_id"], "step_2_tool_2")
        self.check_cells(segment)

    def test_skill_execution_rule_cannot_be_overridden(self):
        document = example("adaptive")
        document["workflow"]["steps"][2]["tools"][1]["execution"] = "always"
        with self.assertRaisesRegex(ValueError, "execution must be conditional"):
            compile_ready(document)

    def test_missing_always_skill_tool_is_rejected(self):
        document = example()
        document["workflow"]["steps"][2]["tools"].pop(0)
        with self.assertRaisesRegex(ValueError, "compute_statistics"):
            compile_ready(document)

    def test_false_and_zero_literals_are_not_treated_as_missing(self):
        document = example()
        document["workflow"]["steps"][1]["tools"][0]["arguments"]["random_state"] = {"source": "literal", "value": 0}
        compiled = compile_ready(document)
        self.assertEqual(compiled["workflow"]["steps"][1]["tools"][0]["arguments"]["random_state"], 0)
        self.assertIs(compiled["workflow"]["context"]["authoring"]["allow_immediate_execution"], False)

    def test_defaults_are_exact_and_input_is_not_shared(self):
        compiled = compile_ready(example("adaptive"))["workflow"]
        merge = compiled["steps"][1]["tools"][0]
        self.assertEqual(merge["arguments"]["suffixes"], ["_left", "_right"])
        self.assertIsNone(merge["arguments"]["validate"])
        self.assertNotEqual(compiled["steps"][0]["tools"][0]["arguments"]["lot_cd"],
                            compiled["steps"][0]["tools"][2]["arguments"]["lot_cd"])

    def test_unknown_and_duplicate_bindings_rejected(self):
        document = example()
        supplied = bindings(document)
        for extra in (supplied[0], {"step_index": 99, "tool_index": 0, "parameter": "x", "value": 1}):
            with self.assertRaisesRegex(ValueError, "Unknown or duplicate"):
                compile_workflow(document, workflow_id=WORKFLOW_ID, execution_inputs=supplied + [extra])

    def test_invalid_references_rejected(self):
        for changes in ({"step_index": 2}, {"tool_index": 99}, {"output": "missing"}, {"step_index": -1}):
            document = example()
            document["workflow"]["steps"][0]["tools"][1]["arguments"]["data"].update(changes)
            with self.assertRaises(ValueError):
                compile_ready(document)

    def test_invalid_skill_tool_argument_and_modes(self):
        for change in ("skill", "tool", "argument", "execution", "mode", "legacy"):
            document = example()
            step = document["workflow"]["steps"][0]
            if change == "skill": step["skill"] = "missing"
            elif change == "tool": step["tools"][0]["tool"] = "missing"
            elif change == "argument": step["tools"][0]["arguments"]["oops"] = {"source": "input"}
            elif change == "execution": step["tools"][0].pop("execution")
            elif change == "mode": document["workflow"]["execution_mode"] = "adaptive"
            else: document["workflow"]["datasets"] = {}
            with self.assertRaises(ValueError):
                compile_ready(document)

    def test_missing_required_parameter_does_not_get_placeholder(self):
        document = example()
        del document["workflow"]["steps"][0]["tools"][0]["arguments"]["data_type"]
        with self.assertRaisesRegex(ValueError, "missing required argument"):
            compile_ready(document)

    def test_data_type_must_match_explicit_transform(self):
        document = example()
        supplied = bindings(document)
        supplied[0]["value"] = "unsupported"
        with self.assertRaisesRegex(ValueError, "requires data_type"):
            compile_workflow(document, workflow_id=WORKFLOW_ID, execution_inputs=supplied)

    def test_conditional_output_cannot_be_required_downstream(self):
        document = example("adaptive")
        tool = document["workflow"]["steps"][2]["tools"][2]
        tool["arguments"]["data"] = {"source": "tool_output", "step_index": 2,
                                      "tool_index": 1, "output": "boxplot_paths"}
        with self.assertRaisesRegex(ValueError, "unavailable"):
            compile_ready(document)

    def test_nearest_condition_tool_must_inspect_same_data(self):
        document = example("adaptive")
        document["workflow"]["steps"][2]["tools"][1]["arguments"]["data"] = {
            "source": "tool_output", "step_index": 0, "tool_index": 1, "output": "data"}
        with self.assertRaisesRegex(ValueError, "same data"):
            compile_ready(document)

    def test_statistics_writes_json_only_when_requested(self):
        self.assertIsNone(inspect.signature(compute_statistics).parameters["output_dir"].default)
        with tempfile.TemporaryDirectory() as directory:
            result = compute_statistics({"x": [1.0, 2.0, 3.0], "label": ["a", "b", "a"]}, output_dir=directory)
            paths = list(Path(directory).glob("compute_statistics__*.json"))
            self.assertEqual(len(paths), 1)
            saved = json.loads(paths[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["statistics"]["x"]["mean"], 2.0)
            self.assertEqual(result["statistics"]["label"]["top"], "a")


if __name__ == "__main__":
    unittest.main()
