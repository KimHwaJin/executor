"""Compile a compact semantic Workflow plan into Workflow schema 1.3."""

from __future__ import annotations

import keyword
import re
from collections import Counter
from copy import deepcopy
from typing import Any

from app.agents.workflow_generator.workflow_generator_tools import (
    _load_skill_index_document,
    _load_tool_registry_document,
    validate_skill_condition_contract,
)
from app.schemas.workflows.workflow_format import (
    InputDefinition,
    WorkflowGeneratorOutput,
)
from app.schemas.workflows.workflow_plan_format import (
    PlanArgument,
    PlanOutputReference,
    WorkflowPlanOutput,
)


def _canonical_workflow_input_type(annotation: Any) -> str:
    """Map Registry annotations to the compact Workflow input type vocabulary."""
    normalized = str(annotation or "").lower().replace(" ", "")
    non_null_parts = [
        part for part in normalized.split("|") if part not in {"none", "null"}
    ]
    normalized = "|".join(non_null_parts)
    if "bool" in normalized:
        return "bool"
    if "int" in normalized:
        return "int"
    if "float" in normalized or "number" in normalized:
        return "float"
    if any(value in normalized for value in ("list", "tuple", "set")):
        return "list"
    if "dict" in normalized or "object" in normalized:
        return "object"
    if "str" in normalized:
        return "str"
    return normalized or "str"


def _reconcile_workflow_input_type(
    *,
    plan_workflow: Any,
    workflow_input_name: str,
    registry_input: dict[str, Any],
    expected_types: dict[str, str],
    tool_name: str,
    tool_input_name: str,
) -> None:
    """Infer/correct Workflow input types and reject conflicting Registry uses."""
    expected_type = _canonical_workflow_input_type(registry_input.get("type"))
    previous_type = expected_types.get(workflow_input_name)
    if previous_type is not None and previous_type != expected_type:
        raise ValueError(
            f"Workflow input {workflow_input_name!r} is connected to incompatible "
            f"Registry types: {previous_type!r} and {expected_type!r} "
            f"({tool_name}.{tool_input_name})"
        )
    expected_types[workflow_input_name] = expected_type

    definition = plan_workflow.input_schema.get(workflow_input_name)
    if definition is None:
        plan_workflow.input_schema[workflow_input_name] = InputDefinition(
            type=expected_type,
            required=True,
            allow_llm_inference=False,
            validation=[],
        )
        return
    if _canonical_workflow_input_type(definition.type) != expected_type:
        definition.type = expected_type


def _non_none_default(input_definition: dict[str, Any]) -> Any:
    """Return a deterministic fallback for an omitted nullable Tool input."""
    annotation = str(input_definition.get("type") or "").lower()
    if "bool" in annotation:
        return False
    if "int" in annotation:
        return 0
    if "float" in annotation:
        return 0.0
    if "list" in annotation or "tuple" in annotation or "set" in annotation:
        return []
    if "dict" in annotation or "object" in annotation:
        return {}
    if "str" in annotation:
        return ""
    return ""


def _registry_default(input_definition: dict[str, Any]) -> Any:
    """Return the Tool Registry default without changing its semantics."""
    return deepcopy(input_definition.get("default"))


def _normalize_optional_collection(
    value: Any,
    input_definition: dict[str, Any],
) -> Any:
    """Preserve the Registry's None-means-all behavior for empty collections."""
    annotation = str(input_definition.get("type") or "").lower()
    if (
        input_definition.get("has_default")
        and input_definition.get("default") is None
        and value == []
        and ("list" in annotation or "tuple" in annotation or "set" in annotation)
    ):
        return None
    return value


def _normalize_default_equivalent_literal(
    value: Any,
    input_definition: dict[str, Any],
) -> Any:
    """Restore the Registry default's type when the LLM stringifies its value."""
    default = input_definition.get("default")
    if (
        input_definition.get("has_default")
        and default is not None
        and not isinstance(default, str)
        and isinstance(value, str)
        and value.strip() == str(default)
    ):
        return deepcopy(default)
    return value


def _normalize_literal(value: Any, input_definition: dict[str, Any]) -> Any:
    value = _normalize_optional_collection(value, input_definition)
    return _normalize_default_equivalent_literal(value, input_definition)


def _identifier(value: str, used: set[str]) -> str:
    candidate = re.sub(r"\W+", "_", value).strip("_") or "value"
    if candidate[0].isdigit():
        candidate = f"v_{candidate}"
    if keyword.iskeyword(candidate):
        candidate = f"{candidate}_value"
    base = candidate
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _tool_id(step_id: str, tool_name: str, counts: Counter[str]) -> str:
    counts[tool_name] += 1
    suffix = "" if counts[tool_name] == 1 else f"_{counts[tool_name]}"
    return f"{step_id}.{tool_name}{suffix}"


def _resolve_step_output(
    reference: PlanArgument | PlanOutputReference,
    *,
    tool_output_refs: dict[tuple[str, str, str], str],
    step_output_refs: dict[tuple[str, str], str],
    external_step_outputs: set[tuple[str, str]] | None = None,
) -> str:
    if reference.tool:
        key = (reference.step_id or "", reference.tool, reference.output or "")
        resolved = tool_output_refs.get(key)
        if resolved is None:
            raise ValueError(
                "Plan references unknown Tool output: "
                f"{key[0]}.{key[1]}.{key[2]}"
            )
        return resolved
    key = (reference.step_id or "", reference.output or "")
    resolved = step_output_refs.get(key)
    if resolved is not None:
        return resolved
    if external_step_outputs is not None and key in external_step_outputs:
        return f"${{steps.{key[0]}.outputs.{key[1]}}}"
    if (
        external_step_outputs is None
        and key[0].startswith("load_data_")
        and key[0].removeprefix("load_data_").isdigit()
        and int(key[0].removeprefix("load_data_")) > 0
        and key[1] == "data"
    ):
        return f"${{steps.{key[0]}.outputs.{key[1]}}}"
    raise ValueError(f"Plan references unknown Step output: {key[0]}.{key[1]}")


def _compile_argument(
    argument: PlanArgument,
    *,
    registry_input: dict[str, Any],
    workflow_inputs: dict[str, Any],
    workflow_input_names: set[str],
    workflow_context: dict[str, Any],
    tool_output_refs: dict[tuple[str, str, str], str],
    step_output_refs: dict[tuple[str, str], str],
    external_step_outputs: set[tuple[str, str]] | None,
) -> tuple[Any, str]:
    if argument.source == "workflow_input":
        assert argument.input_name is not None
        if argument.input_name not in workflow_input_names:
            raise ValueError(
                f"Plan references undeclared Workflow input: {argument.input_name}"
            )
        return f"${{workflow.inputs.{argument.input_name}}}", argument.source
    if argument.source == "context":
        assert argument.context_name is not None
        if argument.context_name not in workflow_context:
            raise ValueError(
                f"Plan references undeclared Workflow context: {argument.context_name}"
            )
        return f"${{workflow.context.{argument.context_name}}}", argument.source
    if argument.source == "step_output":
        return (
            _resolve_step_output(
                argument,
                tool_output_refs=tool_output_refs,
                step_output_refs=step_output_refs,
                external_step_outputs=external_step_outputs,
            ),
            argument.source,
        )
    if argument.source == "default":
        value = (
            argument.value
            if "value" in argument.model_fields_set
            else _registry_default(registry_input)
        )
        return _normalize_literal(value, registry_input), argument.source
    return (
        _normalize_literal(argument.value, registry_input),
        argument.source,
    )


def compile_workflow_plan(
    plan: WorkflowPlanOutput | dict[str, Any],
    *,
    external_step_outputs: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Expand an LLM-authored semantic plan into authoritative schema 1.3."""
    if not isinstance(plan, WorkflowPlanOutput):
        plan = WorkflowPlanOutput.model_validate(plan)
    else:
        plan = plan.model_copy(deep=True)
    plan_workflow = plan.workflow
    skills = _load_skill_index_document().get("skills") or {}
    registry_tools = _load_tool_registry_document().get("tools") or {}
    workflow_inputs = deepcopy(plan_workflow.inputs)
    workflow_input_names = set(plan_workflow.input_schema) | set(workflow_inputs)
    workflow_context = deepcopy(plan_workflow.context)
    used_variables: set[str] = set()
    tool_counts: Counter[str] = Counter()
    tool_output_refs: dict[tuple[str, str, str], str] = {}
    step_output_refs: dict[tuple[str, str], str] = {}
    compiled_steps: list[dict[str, Any]] = []
    has_conditional = False
    workflow_input_types: dict[str, str] = {}

    for step_order, plan_step in enumerate(plan_workflow.steps, start=1):
        skill_definition = skills.get(plan_step.skill)
        if not isinstance(skill_definition, dict):
            raise ValueError(f"Skill Index에 없는 Skill입니다: {plan_step.skill}")
        skill_rules = {
            str(item.get("tool")): item
            for item in skill_definition.get("tools") or []
            if isinstance(item, dict) and item.get("tool")
        }
        selected_names = [tool.tool for tool in plan_step.tools]
        for required_name, rule in skill_rules.items():
            if rule.get("execution") == "always" and required_name not in selected_names:
                raise ValueError(
                    f"{plan_step.skill}.{required_name} is an always Tool, so the "
                    "Plan must include it"
                )

        compiled_tools: list[dict[str, Any]] = []
        output_name_counts: Counter[str] = Counter()
        pending_outputs: list[tuple[str, str, str]] = []
        for tool_order, plan_tool in enumerate(plan_step.tools, start=1):
            definition = registry_tools.get(plan_tool.tool)
            if not isinstance(definition, dict):
                raise ValueError(f"Tool Registry에 없는 Tool입니다: {plan_tool.tool}")
            if plan_tool.tool not in skill_rules:
                raise ValueError(
                    f"Skill {plan_step.skill!r}에 속하지 않는 Tool입니다: "
                    f"{plan_tool.tool}"
                )
            rule = skill_rules[plan_tool.tool]
            condition_tool = str(rule.get("condition_tool") or "없음")
            execution = (
                "conditional"
                if rule.get("execution") == "conditional"
                and condition_tool != "없음"
                else "always"
            )
            has_conditional = has_conditional or execution == "conditional"
            tool_id = _tool_id(plan_step.id, plan_tool.tool, tool_counts)
            registry_inputs = definition.get("inputs") or {}
            unknown_arguments = set(plan_tool.arguments) - set(registry_inputs)
            if unknown_arguments:
                raise ValueError(
                    f"{plan_tool.tool} has unknown Plan arguments: "
                    f"{sorted(unknown_arguments)}"
                )
            arguments: dict[str, Any] = {}
            argument_sources: dict[str, str] = {}
            for input_name, input_definition in registry_inputs.items():
                plan_argument = plan_tool.arguments.get(input_name)
                if plan_argument is None:
                    if not input_definition.get("has_default"):
                        arguments[input_name] = _non_none_default(input_definition)
                    else:
                        arguments[input_name] = _registry_default(input_definition)
                    argument_sources[input_name] = "default"
                    continue
                if plan_argument.source == "workflow_input":
                    assert plan_argument.input_name is not None
                    _reconcile_workflow_input_type(
                        plan_workflow=plan_workflow,
                        workflow_input_name=plan_argument.input_name,
                        registry_input=input_definition,
                        expected_types=workflow_input_types,
                        tool_name=plan_tool.tool,
                        tool_input_name=input_name,
                    )
                value, source = _compile_argument(
                    plan_argument,
                    registry_input=input_definition,
                    workflow_inputs=workflow_inputs,
                    workflow_input_names=workflow_input_names,
                    workflow_context=workflow_context,
                    tool_output_refs=tool_output_refs,
                    step_output_refs=step_output_refs,
                    external_step_outputs=external_step_outputs,
                )
                arguments[input_name] = value
                argument_sources[input_name] = source

            registry_outputs = (definition.get("returns") or {}).get("outputs") or {}
            if not registry_outputs:
                raise ValueError(f"Tool Registry return outputs가 없습니다: {plan_tool.tool}")
            result_variable = _identifier(f"{tool_id}_result", used_variables)
            compiled_outputs: dict[str, dict[str, str]] = {}
            for output_name, output_definition in registry_outputs.items():
                selector = output_definition.get("selector")
                if not isinstance(selector, str) or not selector:
                    raise ValueError(
                        f"Tool Registry selector가 올바르지 않습니다: "
                        f"{plan_tool.tool}.{output_name}"
                    )
                variable = (
                    result_variable
                    if selector == "$" and len(registry_outputs) == 1
                    else _identifier(
                        f"{tool_id}_{output_name}",
                        used_variables,
                    )
                )
                compiled_outputs[output_name] = {
                    "selector": selector,
                    "variable": variable,
                }
                direct_reference = (
                    f"${{steps.{plan_step.id}.tools.{tool_id}.outputs."
                    f"{output_name}}}"
                )
                tool_output_refs[(plan_step.id, plan_tool.tool, output_name)] = (
                    direct_reference
                )
                output_name_counts[output_name] += 1
                pending_outputs.append((plan_tool.tool, output_name, direct_reference))

            source = definition.get("source")
            compiled_tools.append(
                {
                    "id": tool_id,
                    "order": tool_order,
                    "tool": plan_tool.tool,
                    "tool_origin": "registry",
                    "tool_source": f"app/workflow/tools/{source}",
                    "selection_reason": plan_tool.selection_reason,
                    "execution": execution,
                    "condition": None,
                    "arguments": arguments,
                    "argument_sources": argument_sources,
                    "returns": {
                        "result_variable": result_variable,
                        "outputs": compiled_outputs,
                    },
                }
            )

        step_outputs: dict[str, str] = {}
        for tool_name, output_name, reference in pending_outputs:
            alias = (
                output_name
                if output_name_counts[output_name] == 1
                else f"{tool_name}_{output_name}"
            )
            if alias in step_outputs:
                raise ValueError(
                    f"Step output alias가 중복됩니다: {plan_step.id}.{alias}"
                )
            step_outputs[alias] = reference
            step_output_refs[(plan_step.id, alias)] = (
                f"${{steps.{plan_step.id}.outputs.{alias}}}"
            )
        compiled_steps.append(
            {
                "id": plan_step.id,
                "order": step_order,
                "skill": plan_step.skill,
                "skill_source": str(skill_definition["source"]),
                "depends_on": list(plan_step.depends_on),
                "execution": "always",
                "condition": None,
                "tools": compiled_tools,
                "outputs": step_outputs,
            }
        )

    workflow_outputs = {
        name: _resolve_step_output(
            reference,
            tool_output_refs=tool_output_refs,
            step_output_refs=step_output_refs,
            external_step_outputs=external_step_outputs,
        )
        for name, reference in plan_workflow.outputs.items()
    }
    document = {
        "schema_version": "1.3",
        "workflow": {
            "id": plan_workflow.id,
            "name": plan_workflow.name,
            "description": plan_workflow.description,
            "goal": plan_workflow.goal,
            "status": plan_workflow.status.value,
            "execution_mode": "adaptive" if has_conditional else "static",
            "input_schema": {
                name: value.model_dump(mode="json")
                for name, value in plan_workflow.input_schema.items()
            },
            "inputs": workflow_inputs,
            "input_provenance": {
                name: value.model_dump(mode="json")
                for name, value in plan_workflow.input_provenance.items()
            },
            "unresolved_inputs": [
                value.model_dump(mode="json")
                for value in plan_workflow.unresolved_inputs
            ],
            "context": workflow_context,
            "steps": compiled_steps,
            "outputs": workflow_outputs,
        },
    }
    validated = WorkflowGeneratorOutput.model_validate(document).model_dump(mode="json")
    validate_skill_condition_contract(validated)
    return validated


__all__ = ["compile_workflow_plan"]
