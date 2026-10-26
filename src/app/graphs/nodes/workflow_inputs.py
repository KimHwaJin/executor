"""Build a call-addressed input form and validate answers through the compiler."""
from __future__ import annotations

import ast
from copy import deepcopy
import json
from langchain_core.runnables import RunnableConfig

from app.agents.workflow_generator.workflow_standard_compiler import (
    compile_workflow, extract_workflow_inputs,
)
from app.graphs.hitl_protocol import request_human_input


def _type_schema(node: ast.AST) -> dict:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return {"anyOf": [_type_schema(node.left), _type_schema(node.right)]}
    if isinstance(node, ast.Constant) and node.value is None:
        return {"type": "null"}
    if isinstance(node, ast.Name):
        kind = {"str": "string", "int": "integer", "float": "number",
                "bool": "boolean", "list": "array", "tuple": "array",
                "dict": "object", "None": "null"}.get(node.id)
        return {"type": kind} if kind else {}
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        kind = node.value.id
        parts = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
        if kind in {"Optional", "Union"}:
            schemas = [_type_schema(p) for p in parts]
            return {"anyOf": schemas + ([{"type": "null"}] if kind == "Optional" else [])}
        if kind == "list":
            return {"type": "array", "items": _type_schema(parts[0])}
        if kind == "dict":
            return {"type": "object", "additionalProperties": _type_schema(parts[-1])}
        if kind == "tuple":
            return {"type": "array"}
    return {}


def collect_standard_inputs(state: dict, config: RunnableConfig = None) -> dict:
    if state.get("tool_input_payload_version") == 2:
        from app.graphs.nodes.workflow_inputs_v2 import collect_tool_inputs_v2
        return collect_tool_inputs_v2(state, config)
    definition = state["workflow_definition"]
    fields = {f["name"]: f for f in extract_workflow_inputs(definition)}
    pending = {v["name"] for v in state["workflow"]["workflow"]["unresolved_inputs"]}
    current_values = state["workflow"]["workflow"].get("inputs", {})
    provenance = state["workflow"]["workflow"].get("input_provenance", {})
    properties, args = {}, {}
    for name, field in fields.items():
        if name not in pending:
            continue
        schema = _type_schema(ast.parse(field["type"], mode="eval").body)
        if field["nullable"]:
            schema = {"anyOf": [schema, {"type": "null"}]}
        schema.update({
            "title": f"Step {field['step_index']} / Tool {field['tool_index']} · {field['tool']}.{field['parameter']}",
            "description": field["description"] or f"{field['tool']}.{field['parameter']} 입력값",
        })
        if field["has_default"]:
            schema["default"] = deepcopy(field["default"])
        properties[name] = schema
        args[name] = deepcopy(
            current_values[name] if provenance.get(name, {}).get("source") is not None
            else field["default"] if field["has_default"] else None
        )
    description = "선택한 Workflow의 Tool 입력값을 입력해주세요. Step/Tool 번호는 0부터 시작합니다."
    if state.get("input_errors"):
        description += "\n이전 입력 오류: " + state["input_errors"]
    response = request_human_input(
        action_name="additional_information", description=description, args=args,
        allowed_decisions=["approve", "edit"],
        args_schema={"type": "object", "properties": properties,
                     "additionalProperties": False,
                     "required": [name for name in properties if fields[name]["required"]]},
    )
    try:
        answers = response.get("answers", response) if isinstance(response, dict) else response
        if not isinstance(answers, dict):
            raise ValueError("입력값은 파라미터별 JSON 객체로 전달해주세요.")
        if set(answers) - set(properties):
            raise ValueError("현재 입력 화면에 없는 파라미터가 포함되어 있습니다.")
        bindings = {(v["step_index"], v["tool_index"], v["parameter"]): deepcopy(v)
                    for v in state.get("execution_inputs", [])}
        # Submitting the form accepts displayed optional values unless edited.
        answers = {**{name: deepcopy(args[name]) for name in properties
                      if fields[name]["has_default"]}, **answers}
        for name, value in answers.items():
            field = fields[name]
            # Agent Chat may submit a JSON value as text. Never coerce str parameters.
            if isinstance(value, str) and (
                "str" not in field["type"] or field["type"].startswith(("list[", "dict[", "tuple["))
            ):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            key = (field["step_index"], field["tool_index"], field["parameter"])
            bindings[key] = {"step_index": key[0], "tool_index": key[1], "parameter": key[2],
                             "value": value, "origin": "user_answer", "confirmed": True}
        values = list(bindings.values())
        compile_workflow(definition, workflow_id=state["workflow_id"], execution_inputs=values)
    except ValueError as exc:
        return {"input_errors": str(exc), "approval": {}}
    return {"execution_inputs": values, "input_errors": "", "approval": {}}
