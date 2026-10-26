"""Unified mock transformation and notebook embedding contract."""

import ast
from pathlib import Path
import unittest
from unittest.mock import patch

import pandas as pd

from app.workflow.tools.data_io.transform_data import transform_data
from app.test.test_workflow_standard_compiler import example, compile_ready
from app.services.rule_based_notebook_generator import APP_SOURCE_ROOT, build_notebook


class TransformDataTests(unittest.TestCase):
    def frame(self, data_type):
        # Read the existing branch's columns without requiring mock files.
        from app.workflow.tools.data_io.tmp.transform_nce import transform_nce
        from app.workflow.tools.data_io.tmp.transform_wt import transform_wt
        import inspect
        fn = transform_nce if data_type == "nce" else transform_wt
        tree = ast.parse(inspect.getsource(fn))
        assignment = next(n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "expected_columns" for t in n.targets))
        columns = ast.literal_eval(assignment.value)
        return pd.DataFrame({c: [1, 2] for c in columns})

    def test_wide_input_ignored_and_default_mock_loaded(self):
        for dtype, op, filename in (
            ("nce", "pivot", "df_nce_wide_format.parquet"),
            ("wt_symbol", "wt_fail_pivot", "df_wt_symbol_wide_format.parquet"),
        ):
            frame = self.frame(dtype)
            upstream = frame.iloc[:1].to_dict(orient="list")
            with patch("pandas.read_parquet", return_value=frame) as read:
                result = transform_data(dtype.upper(), upstream)
                read.assert_called_once_with(Path("/workspace/pv/data") / filename)
            self.assertEqual(result["data"], frame.to_dict(orient="list"))
            self.assertEqual(result["transform_info"]["transform_op"], op)
            self.assertEqual(result["transform_info"]["steps"], [f"load_mock_wide_{dtype}"])

    def test_mock_branch_and_custom_directory(self):
        for dtype, filename in (("nce", "df_nce_wide_format.parquet"),
                                ("wt_symbol", "df_wt_symbol_wide_format.parquet")):
            with patch("pandas.read_parquet", return_value=self.frame(dtype)) as read:
                result = transform_data(dtype, {}, transform_op="test", mock_data_dir="mock_data")
            read.assert_called_once_with(Path("mock_data") / filename)
            self.assertEqual(result["shape"][0], 2)
            self.assertEqual(result["transform_info"]["transform_op"], "test")

    def test_unsupported_type_rejected_before_io(self):
        with patch("pandas.read_parquet") as read:
            with self.assertRaises(ValueError):
                transform_data("unknown", {})
            read.assert_not_called()

    def unified_example(self, mode):
        document = example(mode)
        for ti, tool in enumerate(document["workflow"]["steps"][0]["tools"]):
            if tool["tool"] in ("transform_nce", "transform_wt"):
                tool["tool"] = "transform_data"
                tool["arguments"]["data_type"] = {
                    "source": "tool_output", "step_index": 0,
                    "tool_index": ti - 1, "output": "data_type",
                }
        return document

    def test_compiler_and_standalone_notebook_cell(self):
        for mode in ("static", "adaptive"):
            compiled = compile_ready(self.unified_example(mode))
            notebook = build_notebook(compiled, project_root=APP_SOURCE_ROOT)
            cells = [c for c in notebook["cells"] if c.get("tool") == "transform_data"]
            self.assertEqual(len(cells), 1 if mode == "static" else 2)
            namespace = {}
            # Resolve emitted variable names from the Registry-generated bindings.
            for ti in (0, 2) if mode == "adaptive" else (0,):
                outputs = compiled["workflow"]["steps"][0]["tools"][ti]["returns"]["outputs"]
                namespace[outputs["data"]["variable"]] = {}
                namespace[outputs["data_type"]["variable"]] = "nce" if ti == 0 else "wt_symbol"
            for ci, cell in enumerate(cells):
                with patch("pandas.read_parquet", return_value=self.frame("nce" if ci == 0 else "wt_symbol")):
                    exec(compile(cell["code"], cell["id"], "exec"), namespace)
                result_var = compiled["workflow"]["steps"][0]["tools"][ci * 2 + 1]["returns"]["result_variable"]
                self.assertEqual(namespace[result_var]["shape"][0], 2)

    def test_mismatched_type_reference_rejected(self):
        document = self.unified_example("adaptive")
        document["workflow"]["steps"][0]["tools"][3]["arguments"]["data_type"]["tool_index"] = 0
        with self.assertRaisesRegex(ValueError, "same extract_data"):
            compile_ready(document)


if __name__ == "__main__":
    unittest.main()
