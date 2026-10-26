"""Deterministic Workflow 1.0 -> Compiled Workflow 1.3 boundary.

Use extract_workflow_inputs(document) to build an input form, then
compile_workflow(document, workflow_id=<catalog UUID>, execution_inputs=[...]).
Each execution input contains step_index, tool_index, parameter, value and
optionally origin (user_request/user_answer) and confirmed. Incomplete values
produce needs_input; they are never replaced with a Tool default. This module
does not call an LLM or persist anything. The graph uses it for generation
validation, candidate selection and execution input binding.
"""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from typing import Any
from uuid import UUID

from app.agents.workflow_generator.workflow_generator_tools import (
    _load_skill_index_document,
    _load_tool_registry_document,
    validate_skill_condition_contract,
)
from app.schemas.workflows.workflow_format import WorkflowGeneratorOutput
from app.schemas.workflows.workflow_standard import (
    ExecutionInput,
    InputArgument,
    LiteralArgument,
    OutputArgument,
    StandardWorkflow,
)
from app.services.adaptive_workflow import conditional_candidate
from app.services.rule_based_notebook_generator import PASSTHROUGH_DATA_OUTPUTS, SELECTOR_RE


def _input_name(si: int, ti: int, parameter: str) -> str:
    return f"step_{si}_tool_{ti}_{parameter}"


def _matches(value: Any, node: ast.AST) -> bool:
    """Interpret only type syntax; never eval/import Registry annotations."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _matches(value, node.left) or _matches(value, node.right)
    if isinstance(node, ast.Constant) and node.value is None:
        return value is None
    if isinstance(node, ast.Name):
        checks = {
            "str": lambda: isinstance(value, str),
            "int": lambda: type(value) is int,
            "float": lambda: type(value) in (int, float),
            "bool": lambda: type(value) is bool,
            "list": lambda: isinstance(value, list),
            "tuple": lambda: isinstance(value, (list, tuple)),
            "dict": lambda: isinstance(value, dict),
            "object": lambda: True,
            "Any": lambda: True,
            "None": lambda: value is None,
        }
        if node.id not in checks:
            raise ValueError(f"Unsupported Registry input type: {node.id}")
        return checks[node.id]()
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        kind = node.value.id
        parts = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
        if kind == "Optional" and len(parts) == 1:
            return value is None or _matches(value, parts[0])
        if kind == "Union":
            return any(_matches(value, p) for p in parts)
        if kind == "list" and len(parts) == 1:
            return isinstance(value, list) and all(_matches(v, parts[0]) for v in value)
        if kind == "dict" and len(parts) == 2:
            return isinstance(value, dict) and all(
                _matches(k, parts[0]) and _matches(v, parts[1]) for k, v in value.items()
            )
        if kind == "tuple":
            if not isinstance(value, (list, tuple)):
                return False
            if len(parts) == 2 and isinstance(parts[1], ast.Constant) and parts[1].value is Ellipsis:
                return all(_matches(v, parts[0]) for v in value)
            return len(value) == len(parts) and all(_matches(v, p) for v, p in zip(value, parts))
    raise ValueError(f"Unsupported Registry input type: {ast.unparse(node)}")


def _nullable(definition: dict) -> bool:
    if definition.get("has_default") and definition.get("default") is None:
        return True
    return _matches(None, ast.parse(str(definition.get("type") or "object"), mode="eval").body)


def _check_value(value: Any, definition: dict, label: str) -> None:
    # Also reject non-JSON objects and NaN/Infinity before rendering Python code.
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: value must be finite JSON") from exc
    if value is None and _nullable(definition):
        return
    annotation = str(definition.get("type") or "object")
    if not _matches(value, ast.parse(annotation, mode="eval").body):
        raise ValueError(f"{label}: expected {annotation}, got {type(value).__name__}")


def _prepare(document: dict | StandardWorkflow):
    plan = StandardWorkflow.model_validate(document).model_copy(deep=True)
    skills = _load_skill_index_document()["skills"]
    registry = _load_tool_registry_document()["tools"]
    for si, step in enumerate(plan.workflow.steps):
        if step.skill not in skills:
            raise ValueError(f"steps[{si}]: unknown Skill {step.skill!r}")
        rules = {r["tool"]: r for r in skills[step.skill]["tools"]}
        for ti, tool in enumerate(step.tools):
            label = f"steps[{si}].tools[{ti}] ({tool.tool})"
            if tool.tool not in rules or tool.tool not in registry:
                raise ValueError(f"{label}: Tool is not registered in Skill {step.skill!r}")
            rule = rules[tool.tool]
            expected = "conditional" if (
                rule["execution"] == "conditional" and rule.get("condition_tool") not in (None, "", "없음")
            ) else "always"
            if tool.execution != expected:
                raise ValueError(f"{label}: execution must be {expected}")
            declared = registry[tool.tool].get("inputs", {})
            unknown = set(tool.arguments) - set(declared)
            if unknown:
                raise ValueError(f"{label}: unknown arguments {sorted(unknown)}")
            for name, definition in declared.items():
                kind = definition.get("kind")
                if kind in ("var_keyword", "var_positional"):
                    if name in tool.arguments:
                        raise ValueError(f"{label}.{name}: explicit variadic arguments are unsupported")
                    continue
                if kind == "positional_only":
                    raise ValueError(f"{label}.{name}: positional-only arguments are unsupported")
                if name not in tool.arguments and not definition.get("has_default"):
                    raise ValueError(f"{label}.{name}: missing required argument")
    return plan, skills, registry


def extract_workflow_inputs(document: dict | StandardWorkflow) -> list[dict[str, Any]]:
    """Return call-addressed input fields; preserve Registry unions and defaults as metadata."""
    plan, _, registry = _prepare(document)
    fields = []
    for si, step in enumerate(plan.workflow.steps):
        for ti, tool in enumerate(step.tools):
            for parameter, argument in tool.arguments.items():
                if isinstance(argument, InputArgument):
                    definition = registry[tool.tool]["inputs"][parameter]
                    fields.append({
                        "step_index": si, "tool_index": ti, "parameter": parameter,
                        "name": _input_name(si, ti, parameter), "tool": tool.tool,
                        "type": definition.get("type") or "object",
                        "required": not bool(definition.get("has_default")),
                        "nullable": _nullable(definition),
                        "description": definition.get("description", ""),
                        "tool_description": registry[tool.tool].get("docstring", ""),
                        "has_default": bool(definition.get("has_default")),
                        "default": deepcopy(definition.get("default")),
                    })
    return fields


def compile_workflow(
    document: dict | StandardWorkflow,
    *,
    workflow_id: str | UUID,
    execution_inputs: list[dict | ExecutionInput] | None = None,
) -> dict[str, Any]:
    """Compile without mutating the reusable definition or inferring missing values.

    workflow_id is the catalog UUID supplied by the caller, not generated here.
    Recompile with the same UUID and additional execution inputs after form submission.
    """
    catalog_id = str(UUID(str(workflow_id)))
    plan, skills, registry = _prepare(document)
    fields = extract_workflow_inputs(plan)
    declared_inputs = {(f["step_index"], f["tool_index"], f["parameter"]): f for f in fields}
    bindings = {}
    for raw in execution_inputs or []:
        binding = ExecutionInput.model_validate(raw)
        key = (binding.step_index, binding.tool_index, binding.parameter)
        if key not in declared_inputs or key in bindings:
            raise ValueError(f"Unknown or duplicate execution input: {key}")
        bindings[key] = binding

    inputs, provenance, input_schema, unresolved = {}, {}, {}, []
    for key, field in declared_inputs.items():
        name = field["name"]
        # 1.3 rejects required=None. Presence is enforced here for nullable values,
        # while the compiled schema marks them optional to preserve explicit null.
        input_schema[name] = {
            "type": field["type"], "required": not field["nullable"],
            "allow_llm_inference": False, "validation": [],
        }
        binding = bindings.get(key)
        if binding is not None:
            definition = registry[field["tool"]]["inputs"][field["parameter"]]
            _check_value(binding.value, definition, name)
            inputs[name] = deepcopy(binding.value)
            provenance[name] = {
                "source": "user_query" if binding.origin == "user_request" else "user_answer",
                "confirmed": binding.confirmed,
            }
        if binding is None or not binding.confirmed:
            inputs.setdefault(name, None)
            provenance.setdefault(name, {"source": None, "confirmed": False})
            unresolved.append({
                "name": name, "question": f"{field['tool']}.{field['parameter']} 값을 입력해주세요.",
                "required_for": [f"step_{key[0]}_tool_{key[1]}"],
            })

    refs, calls, safe_outputs = {}, {}, set()
    compiled_steps, final_outputs = [], {}
    for si, step in enumerate(plan.workflow.steps):
        step_id = f"step_{si}"
        compiled_tools, step_outputs = [], {}
        for ti, tool in enumerate(step.tools):
            tool_id = f"step_{si}_tool_{ti}"
            definition = registry[tool.tool]
            arguments, sources = {}, {}
            for parameter, meta in definition.get("inputs", {}).items():
                if meta.get("kind") in ("var_keyword", "var_positional"):
                    continue
                arg = tool.arguments.get(parameter)
                label = f"{tool_id}.{parameter}"
                if arg is None:
                    value = deepcopy(meta["default"])
                    _check_value(value, meta, label)
                    arguments[parameter], sources[parameter] = value, "default"
                elif isinstance(arg, LiteralArgument):
                    _check_value(arg.value, meta, label)
                    arguments[parameter], sources[parameter] = deepcopy(arg.value), "planner"
                elif isinstance(arg, InputArgument):
                    arguments[parameter] = "${workflow.inputs." + _input_name(si, ti, parameter) + "}"
                    sources[parameter] = "workflow_input"
                else:
                    key = (arg.step_index, arg.tool_index, arg.output)
                    if key not in refs:
                        raise ValueError(f"{label}: unknown or forward output reference {key}")
                    if key not in safe_outputs:
                        raise ValueError(f"{label}: output may be unavailable when conditional Tool is excluded: {key}")
                    arguments[parameter], sources[parameter] = refs[key], "step_output"

            self_call = {"tool": tool.tool, "arguments": arguments, "argument_sources": sources}
            _validate_transform(self_call, calls, tool.arguments, bindings, si, ti)
            returns = (definition.get("returns") or {}).get("outputs") or {}
            if not returns:
                raise ValueError(f"{tool_id}: Registry has no named outputs")
            result_name = f"{tool_id}_result"
            output_bindings = {}
            for oi, (output, meta) in enumerate(returns.items()):
                selector = meta.get("selector")
                if not isinstance(selector, str) or (selector != "$" and not SELECTOR_RE.fullmatch(selector)):
                    raise ValueError(f"{tool_id}.{output}: unsupported Registry selector")
                variable = result_name if selector == "$" else f"{tool_id}_output_{oi}"
                output_bindings[output] = {"selector": selector, "variable": variable}
                key = (si, ti, output)
                refs[key] = f"${{steps.{step_id}.tools.{tool_id}.outputs.{output}}}"
                alias = f"tool_{ti}_output_{oi}"
                step_outputs[alias] = refs[key]
                if tool.execution == "always":
                    safe_outputs.add(key)
                    final_outputs[f"{step_id}_{alias}"] = refs[key]
                elif output in PASSTHROUGH_DATA_OUTPUTS and sources.get("data") in ("workflow_input", "step_output"):
                    safe_outputs.add(key)
            compiled_tools.append({
                "id": tool_id, "order": ti + 1, "tool": tool.tool,
                "tool_origin": "registry", "tool_source": f"app/workflow/tools/{definition['source']}",
                "selection_reason": "Workflow에 명시된 Tool 호출",
                "execution": tool.execution, "condition": None,
                "arguments": arguments, "argument_sources": sources,
                "returns": {"result_variable": result_name, "outputs": output_bindings},
            })
            calls[(si, ti)] = self_call
        compiled_steps.append({
            "id": step_id, "order": si + 1, "skill": step.skill,
            "skill_source": skills[step.skill]["source"], "depends_on": [],
            "execution": "always", "condition": None, "tools": compiled_tools,
            "outputs": step_outputs,
        })
    if not final_outputs:
        raise ValueError("At least one always Tool with a named output is required by schema 1.3")
    result = {"schema_version": "1.3", "workflow": {
        "id": catalog_id, "name": plan.workflow.name,
        "description": plan.workflow.user_request, "goal": plan.workflow.user_request,
        "status": "needs_input" if unresolved else "ready",
        "execution_mode": plan.workflow.execution_mode,
        "input_schema": input_schema, "inputs": inputs, "input_provenance": provenance,
        "unresolved_inputs": unresolved,
        "context": {"authoring": {
            "workflow_version": plan.workflow_version,
            "actor": plan.workflow.actor.model_dump(),
            "allow_immediate_execution": plan.workflow.allow_immediate_execution,
        }},
        "steps": compiled_steps, "outputs": final_outputs,
    }}
    result = WorkflowGeneratorOutput.model_validate(result).model_dump(mode="json")
    validate_skill_condition_contract(result)
    # Match the runtime's nearest preceding condition Tool, and reject ambiguous
    # diagnostics of another dataset instead of silently using them.
    by_id = {t["id"]: t for s in result["workflow"]["steps"] for t in s["tools"]}
    for tool in by_id.values():
        if tool["execution"] == "conditional":
            condition = conditional_candidate(result, tool["id"])
            diagnostic = by_id[condition["condition_tool_id"]]
            if diagnostic["execution"] != "always":
                raise ValueError("A conditional decision requires an always condition Tool")
            if "data" in tool["arguments"] and "data" in diagnostic["arguments"]:
                if tool["arguments"]["data"] != diagnostic["arguments"]["data"]:
                    raise ValueError(f"{tool['id']}: condition Tool must inspect the same data")
    return result


def _validate_transform(call, calls, authored_arguments, bindings, si, ti):
    expected = {"transform_nce": "nce", "transform_wt": "wt_symbol"}.get(call["tool"])
    unified = call["tool"] == "transform_data"
    if expected is None and not unified:
        return
    data = authored_arguments.get("data")
    if not isinstance(data, OutputArgument) or data.output != "data":
        raise ValueError(f"{call['tool']}: data must reference extract_data.data")
    source = calls.get((data.step_index, data.tool_index))
    if source is None or source["tool"] != "extract_data":
        raise ValueError(f"{call['tool']}: data must reference extract_data.data")
    if unified:
        type_argument = authored_arguments.get("data_type")
        if isinstance(type_argument, OutputArgument):
            if (type_argument.step_index, type_argument.tool_index, type_argument.output) != (
                data.step_index, data.tool_index, "data_type"
            ):
                raise ValueError("transform_data.data_type must reference the same extract_data call")
        elif isinstance(type_argument, InputArgument):
            bound = bindings.get((si, ti, "data_type"))
            expected = bound.value if bound is not None and bound.confirmed else None
        else:
            expected = call["arguments"].get("data_type")
        if expected is not None:
            if not isinstance(expected, str) or expected.strip().lower() not in {"nce", "wt_symbol"}:
                raise ValueError("transform_data requires data_type nce or wt_symbol")
            expected = expected.strip().lower()
    dtype = source["arguments"].get("data_type")
    if source["argument_sources"].get("data_type") == "workflow_input":
        binding = bindings.get((data.step_index, data.tool_index, "data_type"))
        if binding is None or not binding.confirmed:
            return
        dtype = binding.value
    if unified and expected is None:
        if not isinstance(dtype, str) or dtype.strip().lower() not in {"nce", "wt_symbol"}:
            raise ValueError("transform_data requires data_type nce or wt_symbol")
        return
    if not isinstance(dtype, str) or dtype.strip().lower() != expected:
        raise ValueError(f"step {si} tool {ti}: {call['tool']} requires data_type={expected!r}")


__all__ = ["compile_workflow", "extract_workflow_inputs"]
