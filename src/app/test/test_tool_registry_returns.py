"""Output discovery does not execute tools or expressions in their documentation."""
import ast
import runpy
import warnings
from pathlib import Path

import unittest


GENERATOR = runpy.run_path(str(
    Path(__file__).resolve().parents[1] / "workflow/tools/generate_tool_registry.py"
))


def returns(doc, body="return opaque_result()"):
    source = f"def example() -> dict[str, object]:\n    {doc!r}\n"
    source += "\n".join("    " + line for line in body.splitlines())
    return GENERATOR["_returns"](ast.parse(source).body[0])["outputs"]


class ToolRegistryReturnsTests(unittest.TestCase):

    def test_documented_dict_shapes(self):
        cases = [
            ('Returns:\n    ```{"data":dict, "transform_info":dict}```', ['data', 'transform_info']),
            ('Returns:\n    추출+로드 결과 dict:\n    {"data": dict, "metadata":dict, "preview_data": list}',
             ['data', 'metadata', 'preview_data']),
            ('Returns:\n    ```python\n    {\n        "data": dict,\n        "metadata": {"inner": str}\n    }\n    ```',
             ['data', 'metadata']),
            ('Returns: {"data": dict[str, object], "shape": list[int]}', ['data', 'shape']),
            ('Returns:\n    {"data": "brace } within string", "metadata": dict}', ['data', 'metadata']),
            ('Returns:\n    {"data": dict}\n    동일한 형식: {"data": object}', ['data']),
        ]
        for doc, keys in cases:
            with self.subTest(case=doc):
                outputs = returns(doc)
                assert list(outputs) == keys
                assert outputs[keys[0]] == {'selector': f'["{keys[0]}"]'}

    def test_actual_code_takes_precedence(self):
        for body in ['return {"actual": 1}', 'result = {"actual": 1}\nreturn result']:
            with self.subTest(case=body):
                with warnings.catch_warnings():
                    warnings.simplefilter('error')
                    assert returns('Returns:\n    {"other": dict}\n    {"different": dict}', body) == {'actual': {'selector': '["actual"]'}}

    def test_absent_shape_preserves_whole_result(self):
        for doc in [
            'Args:\n    config: {"wrong": dict}\nReturns:\n    DataFrame\nExamples:\n    {"also_wrong": dict}',
            'Return a result.\nExamples:\n    {"wrong": dict}',
            'Returns:\n    dict[str, object]',
        ]:
            with self.subTest(case=doc):
                assert returns(doc) == {'result': {'selector': '$'}}

    def test_ambiguous_or_unsupported_shapes_warn(self):
        for shape in [
            '{"data": dict}\n    {"error": str}',
            '{"data": dict,',
            '{"data": dict, **other}',
            '{computed_key: dict}',
            '{"data": dict, 1: str}',
            '{"metadata": {"nested": dict}, broken}',
        ]:
            with self.subTest(case=shape):
                with self.assertWarnsRegex(UserWarning, 'example: ambiguous or unsupported'):
                    assert returns('Returns:\n    ' + shape) == {'result': {'selector': '$'}}

    def test_docstring_expressions_are_never_executed(self):
        assert returns('Returns:\n    {"data": never_call_this()}') == {'data': {'selector': '["data"]'}}

    def test_quoted_key_selector_is_escaped(self):
        output = returns('Returns:\n    {\'a"b\': dict}')
        selector = output['a"b']['selector']
        assert ast.literal_eval(selector) == ['a"b']

if __name__ == "__main__":
    unittest.main()
