"""Deterministic data-load steps required at the start of every Workflow."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from agent_config import resolve_mock_data_path
from app.schemas.workflows.workflow_format import WorkflowGeneratorOutput


LOCAL_TOOL_REF_RE = re.compile(
    r"^\$\{tools\.([^.{}]+)\.outputs\.([^.{}]+)\}$"
)
WORKFLOW_REF_RE = re.compile(r"^\$\{(steps\.[^{}]+)\}$")
TRANSFORM_BY_DATA_TYPE = {
    "nce": "transform_nce",
    "wt_symbol": "transform_wt",
}


def _remember_output_alias(
    aliases: dict[str, str | None],
    value: Any,
    reference: str,
    *,
    prefer: bool = False,
) -> None:
    if not isinstance(value, str):
        return
    alias = value.strip()
    if not alias or alias.startswith("${"):
        return
    if prefer:
        aliases[alias] = reference
    elif alias not in aliases:
        aliases[alias] = reference
    elif aliases[alias] != reference:
        aliases[alias] = None


def _resolve_output_alias(
    aliases: dict[str, str | None],
    value: Any,
) -> str | None:
    if not isinstance(value, str):
        return None
    resolved = aliases.get(value.strip())
    return resolved if isinstance(resolved, str) else None


def _remember_tool_output_aliases(
    aliases: dict[str, str | None],
    step_id: str,
    tool: dict[str, Any],
) -> None:
    tool_id = tool["id"]
    returns = tool.get("returns") or {}
    outputs = returns.get("outputs") or {}
    for output_name, output in outputs.items():
        reference = f"${{steps.{step_id}.tools.{tool_id}.outputs.{output_name}}}"
        if isinstance(output, dict):
            _remember_output_alias(aliases, output.get("variable"), reference)

    if len(outputs) == 1:
        output_name, output = next(iter(outputs.items()))
        if isinstance(output, dict) and output.get("selector") == "$":
            reference = f"${{steps.{step_id}.tools.{tool_id}.outputs.{output_name}}}"
            _remember_output_alias(
                aliases,
                returns.get("result_variable"),
                reference,
            )


def _remember_step_output_aliases(
    aliases: dict[str, str | None],
    step_id: str,
    outputs: dict[str, Any],
) -> None:
    for output_name in outputs:
        reference = f"${{steps.{step_id}.outputs.{output_name}}}"
        _remember_output_alias(aliases, output_name, reference, prefer=True)


def required_data_load_steps(
    selection: dict[str, Any],
    *,
    data_mock: bool = False,
) -> list[dict[str, Any]]:
    """Create deterministic extract-and-transform steps for selected data."""
    steps: list[dict[str, Any]] = []
    for index, dataset in enumerate(selection["datasets"], start=1):
        step_id = f"load_data_{index}"
        extract_tool_id = f"extract_dataset_{index}"
        transform_tool_id = f"transform_dataset_{index}"
        data_type = str(dataset["data_type"]).lower()
        try:
            transform_name = TRANSFORM_BY_DATA_TYPE[data_type]
        except KeyError as error:
            supported = ", ".join(sorted(TRANSFORM_BY_DATA_TYPE))
            raise ValueError(
                f"Unsupported data_type {data_type!r}; supported: {supported}"
            ) from error
        transform_source = f"app/workflow/tools/data_io/{transform_name}.py"
        extracted_variable = f"extracted_data_{index}"
        variable = f"df_dataset_{index}"
        if data_mock:
            mock_path = str(resolve_mock_data_path(data_type))
            steps.append(
                {
                    "id": step_id,
                    "order": index,
                    "skill": "data_load",
                    "skill_source": "app/workflow/skills/data_io/data_load.md",
                    "depends_on": [],
                    "execution": "always",
                    "tools": [
                        {
                            "id": f"load_dataset_{index}",
                            "order": 1,
                            "tool": "data_load",
                            "tool_origin": "registry",
                            "tool_source": "app/workflow/tools/data_io/data_load.py",
                            "selection_reason": (
                                f"DATA_MOCK is enabled; load the prepared wide "
                                f"{dataset['role'].upper()} parquet directly."
                            ),
                            "execution": "always",
                            "arguments": {"parquet_path": mock_path},
                            "argument_sources": {"parquet_path": "planner"},
                            "returns": {
                                "result_variable": variable,
                                "outputs": {
                                    "data": {"selector": "$", "variable": variable}
                                },
                            },
                        }
                    ],
                    "outputs": {
                        "data": (
                            f"${{steps.{step_id}.tools.load_dataset_{index}."
                            "outputs.data}"
                        )
                    },
                }
            )
            continue
        steps.append(
            {
                "id": step_id,
                "order": index,
                "skill": "data_load",
                "skill_source": "app/workflow/skills/data_io/data_load.md",
                "depends_on": [],
                "execution": "always",
                "tools": [
                    {
                        "id": extract_tool_id,
                        "order": 1,
                        "tool": "extract_data",
                        "tool_origin": "registry",
                        "tool_source": "app/workflow/tools/data_io/extract_data.py",
                        "selection_reason": (
                            f"Extract selected {dataset['role'].upper()} "
                            f"{dataset['data_type']} data."
                        ),
                        "execution": "always",
                        "arguments": {
                            name: dataset.get(name) if name == "wafer_list" else dataset[name]
                            for name in (
                                "data_type",
                                "lot_cd",
                                "process",
                                "query_mode",
                                "start_dt",
                                "end_dt",
                                "wafer_list",
                                "limit",
                            )
                        },
                        "argument_sources": {
                            name: "planner"
                            for name in (
                                "data_type",
                                "lot_cd",
                                "process",
                                "query_mode",
                                "start_dt",
                                "end_dt",
                                "wafer_list",
                                "limit",
                            )
                        },
                        "returns": {
                            "result_variable": f"extract_result_{index}",
                            "outputs": {
                                "data": {
                                    "selector": '["data"]',
                                    "variable": extracted_variable,
                                }
                            },
                        },
                    },
                    {
                        "id": transform_tool_id,
                        "order": 2,
                        "tool": transform_name,
                        "tool_origin": "registry",
                        "tool_source": transform_source,
                        "selection_reason": (
                            f"Transform extracted {dataset['role'].upper()} data "
                            "into the analysis-ready wide format."
                        ),
                        "execution": "always",
                        "arguments": {
                            "data": (
                                f"${{steps.{step_id}.tools."
                                f"{extract_tool_id}.outputs.data}}"
                            ),
                            "transform_op": dataset["transform_op"],
                        },
                        "argument_sources": {
                            "data": "step_output",
                            "transform_op": "planner",
                        },
                        "returns": {
                            "result_variable": f"transform_result_{index}",
                            "outputs": {
                                "data": {
                                    "selector": '["data"]',
                                    "variable": variable,
                                }
                            },
                        },
                    },
                ],
                "outputs": {
                    "data": (
                        f"${{steps.{step_id}.tools."
                        f"{transform_tool_id}.outputs.data}}"
                    )
                },
            }
        )
    return steps


def _reference_origins(value: Any, origins: dict[str, set[str]]) -> set[str]:
    if isinstance(value, str):
        match = WORKFLOW_REF_RE.fullmatch(value)
        return set(origins.get(match.group(1), set())) if match else set()
    if isinstance(value, dict):
        return set().union(
            *(_reference_origins(item, origins) for item in value.values()),
            set(),
        )
    if isinstance(value, (list, tuple)):
        return set().union(
            *(_reference_origins(item, origins) for item in value),
            set(),
        )
    return set()


def validate_target_data_role_lineage(
    document: dict[str, Any],
    selection: dict[str, Any],
) -> None:
    """Reject target-aware Tools fed only by X-role data.

    Y-only analysis remains valid. A merge is therefore not mandatory, but
    every data-like input of a Tool using target_column/target_col must derive
    from at least one selected Y dataset.
    """

    role_by_load_step = {
        f"load_data_{index}": str(dataset["role"])
        for index, dataset in enumerate(selection["datasets"], start=1)
    }
    origins: dict[str, set[str]] = {}
    for step in sorted(
        document["workflow"].get("steps") or [],
        key=lambda item: item.get("order", 0),
    ):
        step_id = str(step["id"])
        for tool in sorted(
            step.get("tools") or [], key=lambda item: item.get("order", 0)
        ):
            tool_id = str(tool["id"])
            arguments = tool.get("arguments") or {}
            tool_origins = set().union(
                *(
                    _reference_origins(value, origins)
                    for value in arguments.values()
                ),
                set(),
            )
            if step_id in role_by_load_step and tool.get("tool") in {
                "data_load",
                "extract_data",
                "transform_nce",
                "transform_wt",
            }:
                tool_origins = {role_by_load_step[step_id]}

            target_argument_names = {
                name
                for name in ("target_column", "target_col")
                if arguments.get(name) is not None
            }
            if target_argument_names:
                data_arguments = {
                    name: value
                    for name, value in arguments.items()
                    if name == "data" or name.endswith("_data")
                }
                if not data_arguments:
                    raise ValueError(
                        f"{step_id}.{tool_id}: target-aware Tool has no data input"
                    )
                for name, value in data_arguments.items():
                    roles = _reference_origins(value, origins)
                    if "y" not in roles:
                        raise ValueError(
                            f"{step_id}.{tool_id}.{name}: target-aware Tool input "
                            "must derive from a role='y' dataset, either Y-only or "
                            f"merged X/Y data; resolved roles={sorted(roles)}"
                        )

            for output_name in (tool.get("returns") or {}).get("outputs", {}):
                origins[
                    f"steps.{step_id}.tools.{tool_id}.outputs.{output_name}"
                ] = set(tool_origins)

        for output_name, value in (step.get("outputs") or {}).items():
            origins[f"steps.{step_id}.outputs.{output_name}"] = (
                _reference_origins(value, origins)
            )


def merge_required_data_load_steps(
    document: dict[str, Any],
    selection: dict[str, Any],
    *,
    data_mock: bool = False,
) -> dict[str, Any]:
    """Prepend deterministic data-load inputs and Steps to an agent Workflow."""
    merged = deepcopy(document)
    workflow = merged["workflow"]
    required_steps = required_data_load_steps(selection, data_mock=data_mock)
    required_step_ids = {step["id"] for step in required_steps}
    required_tool_ids = {
        tool["id"] for step in required_steps for tool in step["tools"]
    }

    agent_steps = workflow.get("steps", [])
    if any(
        step.get("skill") == "data_load"
        or any(tool.get("tool") == "data_load" for tool in step.get("tools", []))
        for step in agent_steps
    ):
        raise ValueError(
            "Workflow Generator must not generate data_load Steps; "
            "they are added deterministically by code"
        )

    workflow_inputs = workflow.get("inputs", {})
    workflow_context = workflow.get("context", {})

    output_aliases: dict[str, str | None] = {}
    for step in required_steps:
        for tool in step.get("tools", []):
            _remember_tool_output_aliases(output_aliases, step["id"], tool)

    # Some models shorten a same-Step Tool output to `${tools...}`. The
    # Workflow contract requires an absolute `${steps.<step>.tools...}` path,
    # so canonicalize the unambiguous local form before persisting it.
    for step in agent_steps:
        step_id = step["id"]
        tool_outputs = {
            tool["id"]: set((tool.get("returns") or {}).get("outputs", {}))
            for tool in step.get("tools", [])
        }

        def canonical_reference(value: Any) -> Any:
            match = LOCAL_TOOL_REF_RE.fullmatch(value) if isinstance(value, str) else None
            if match is None:
                return value
            tool_id, output_name = match.groups()
            if output_name not in tool_outputs.get(tool_id, set()):
                raise ValueError(
                    f"{step_id}: unknown local Tool output reference {value!r}"
                )
            return (
                f"${{steps.{step_id}.tools.{tool_id}.outputs.{output_name}}}"
            )

        for tool in step.get("tools", []):
            arguments = tool.get("arguments", {})
            sources = tool.get("argument_sources", {})
            normalized_arguments: dict[str, Any] = {}
            for name, value in arguments.items():
                value = canonical_reference(value)
                source = sources.get(name)
                is_reference = isinstance(value, str) and value.startswith("${")
                if source == "workflow_input" and not is_reference:
                    if name not in workflow_inputs or workflow_inputs[name] != value:
                        raise ValueError(
                            f"{step_id}.{tool['id']}.{name}: workflow_input source "
                            "must match a Workflow input"
                        )
                    value = f"${{workflow.inputs.{name}}}"
                elif source == "context" and not is_reference:
                    if name not in workflow_context or workflow_context[name] != value:
                        raise ValueError(
                            f"{step_id}.{tool['id']}.{name}: context source must "
                            "match Workflow context"
                        )
                    value = f"${{workflow.context.{name}}}"
                elif source == "step_output" and not is_reference:
                    resolved = _resolve_output_alias(output_aliases, value)
                    if resolved is None:
                        raise ValueError(
                            f"{step_id}.{tool['id']}.{name}: step_output source "
                            "requires a Workflow reference"
                        )
                    value = resolved
                normalized_arguments[name] = value
            tool["arguments"] = normalized_arguments
            _remember_tool_output_aliases(output_aliases, step_id, tool)
        step["outputs"] = {
            name: canonical_reference(value)
            for name, value in step.get("outputs", {}).items()
        }
        _remember_step_output_aliases(output_aliases, step_id, step["outputs"])

    agent_step_ids = {step["id"] for step in agent_steps}
    agent_tool_ids = {
        tool["id"] for step in agent_steps for tool in step.get("tools", [])
    }
    if required_step_ids & agent_step_ids:
        raise ValueError("generated Workflow uses a reserved data_load Step ID")
    if required_tool_ids & agent_tool_ids:
        raise ValueError("generated Workflow uses a reserved data_load Tool ID")

    load_ids = [step["id"] for step in required_steps]
    for order, step in enumerate(agent_steps, start=len(required_steps) + 1):
        step["order"] = order
        if not step.get("depends_on"):
            step["depends_on"] = load_ids.copy()
    workflow["steps"] = required_steps + agent_steps

    return WorkflowGeneratorOutput.model_validate(merged).model_dump(mode="json")


__all__ = [
    "merge_required_data_load_steps",
    "required_data_load_steps",
    "validate_target_data_role_lineage",
]
