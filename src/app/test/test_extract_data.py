"""Extraction argument contract from data_selection.xlsx."""

import inspect
import unittest

from app.agents.workflow_generator.workflow_generator_tools import _load_tool_registry_document
from app.test.test_workflow_standard_compiler import example, compile_ready
from app.workflow.tools.data_io.extract_data import extract_data


class ExtractDataTests(unittest.TestCase):
    def query(self):
        return {"data_type": "nce", "query_mode": "period", "lot_cd": "LOT1",
                "process": ["ALL"], "start_dt": "2026-08-01", "end_dt": "2026-08-02",
                "wafer_list": None}

    def test_required_arguments_cannot_be_omitted(self):
        for name in self.query():
            query = self.query()
            del query[name]
            with self.subTest(missing=name), self.assertRaises(TypeError):
                extract_data(**query)

    def test_compiler_rejects_omitted_required_arguments(self):
        for name in self.query():
            document = example()
            del document["workflow"]["steps"][0]["tools"][0]["arguments"][name]
            with self.subTest(missing=name), self.assertRaisesRegex(ValueError, "missing required argument"):
                compile_ready(document)

    def test_optional_filters_and_inactive_dates_are_preserved(self):
        query = {**self.query(), "query_mode": "wafer_list", "start_dt": None,
                 "end_dt": None, "wafer_list": ["W01", "W02"],
                 "title": ["TITLE"], "symbol": ["EB"], "bin": [1, 2],
                 "device": "DEVICE", "owner": "OWNER", "program": "PROGRAM",
                 "equip": "EQUIP", "limit": 0}
        result = extract_data(**query)
        self.assertEqual(result["query_params"], {k: v for k, v in query.items() if k != "data_type"})
        self.assertTrue(result["metadata"]["placeholder"])

    def test_registry_exposes_all_fifteen_parameters_with_matching_requiredness(self):
        inputs = _load_tool_registry_document()["tools"]["extract_data"]["inputs"]
        parameters = inspect.signature(extract_data).parameters
        self.assertEqual(set(inputs) - {"kwargs"}, set(self.query()) | {
            "title", "symbol", "bin", "device", "owner", "program", "equip", "limit"})
        for name in set(inputs) - {"kwargs"}:
            with self.subTest(parameter=name):
                self.assertEqual(inputs[name]["has_default"],
                                 parameters[name].default is not inspect.Parameter.empty)
        self.assertEqual({name for name, value in inputs.items()
                          if name != "kwargs" and not value["has_default"]}, set(self.query()))

    def test_static_workflow_compiles_to_callable_extract_arguments(self):
        compiled = compile_ready(example())
        arguments = compiled["workflow"]["steps"][0]["tools"][0]["arguments"]
        values = compiled["workflow"]["inputs"]
        bound = {name: values[value.removeprefix("${inputs.").removesuffix("}")]
                 if isinstance(value, str) and value.startswith("${inputs.") else value
                 for name, value in arguments.items()}
        result = extract_data(**bound)
        self.assertEqual(result["query_params"]["query_mode"], "period")
        self.assertIsNone(result["query_params"]["wafer_list"])


if __name__ == "__main__":
    unittest.main()
