"""Rule-based Workflow JSON to executor cell code generator."""

from __future__ import annotations

import argparse
import ast
import keyword
import re
from pathlib import Path
from typing import Any

import yaml

APP_SOURCE_ROOT = Path(__file__).resolve().parents[2]
SUPPORTED_SCHEMA_VERSIONS = {"1.1", "1.2", "1.3"}
REF_RE = re.compile(r"^\$\{([^{}]+)\}$")
SELECTOR_RE = re.compile(r'^(?:\["[^"\\]+"\])*$')
PASSTHROUGH_DATA_OUTPUTS = {
    "data",
    "imputed_data",
    "data_without_outliers",
    "selected_data",
}
NO_RESULT_PRINT_TOOLS = {"extract_data", "transform_nce", "transform_wt", "transform_data"}
SUMMARY_ONLY_PRINT_TOOLS = {"merge_data": "merge_summary"}


class WorkflowValidationError(ValueError):
    """Raised when a workflow cannot safely be converted."""


def _result_print_lines(function_name: str, result_variable: str) -> list[str]:
    if function_name in SUMMARY_ONLY_PRINT_TOOLS:
        summary_key = SUMMARY_ONLY_PRINT_TOOLS[function_name]
        return [f'print({result_variable}["{summary_key}"])']
    if function_name in NO_RESULT_PRINT_TOOLS:
        return []
    return [f"print({result_variable})"]


def _cell(
    code: str,
    *,
    skill: str | None,
    role: str,
    cell_id: str,
    step_id: str | None = None,
    tool_id: str | None = None,
    tool: str | None = None,
) -> dict[str, Any]:
    """Executor가 소비하는 단일 실행 셀을 만든다."""
    return {
        "order": 0,
        "skill": skill,
        "step_id": step_id,
        "tool_id": tool_id,
        "tool": tool,
        "cell_type": "code",
        "role": role,
        "id": cell_id,
        "code": code,
    }


def _identifier(value: Any, label: str, used: set[str]) -> str:
    if not isinstance(value, str) or not value.isidentifier() or keyword.iskeyword(value):
        raise WorkflowValidationError(f"{label}: invalid Python identifier {value!r}")
    if value in used:
        raise WorkflowValidationError(f"duplicate notebook variable: {value}")
    used.add(value)
    return value


def _reference(
    value: Any,
    refs: dict[str, str],
    label: str,
    unavailable_refs: set[str] | None = None,
) -> str:
    match = REF_RE.fullmatch(value) if isinstance(value, str) else None
    if not match or match.group(1) not in refs:
        raise WorkflowValidationError(f"{label}: unknown or forward reference {value!r}")
    if unavailable_refs is not None and match.group(1) in unavailable_refs:
        raise WorkflowValidationError(
            f"{label}: reference depends on an excluded conditional Tool {value!r}"
        )
    return refs[match.group(1)]


def _argument(
    value: Any,
    source: str,
    refs: dict[str, str],
    label: str,
    unavailable_refs: set[str] | None = None,
) -> str:
    if source in {"workflow_input", "context", "step_output"}:
        return _reference(value, refs, label, unavailable_refs)
    if source in {"default", "planner"}:
        return repr(value)
    raise WorkflowValidationError(f"{label}: unsupported argument source {source!r}")


def _function_source(path: Path, function_name: str) -> str:
    if path.suffix != ".py" or not path.is_file():
        raise WorkflowValidationError(f"tool source is not a Python file: {path}")
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise WorkflowValidationError(f"invalid tool source {path}: {exc}") from exc
    found = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    ]
    if len(found) != 1:
        raise WorkflowValidationError(
            f"{path}: expected exactly one function named {function_name!r}"
        )
    return source.rstrip() + "\n"


def build_notebook(
    workflow_path: str | Path | dict[str, Any],
    project_root: str | Path | None = None,
    job_id: str = "",
    *,
    selected_tool: tuple[str, str] | None = None,
    executed_tool_ids: set[str] | None = None,
    runtime_decisions: dict[str, str] | None = None,
    allow_empty_adaptive_segment: bool = False,
) -> dict[str, Any]:
    """Build a static batch, an adaptive prefix, or one selected adaptive cell."""
    if isinstance(workflow_path, dict):
        if project_root is None:
            raise WorkflowValidationError(
                "project_root is required when Workflow JSON is provided"
            )
        root = Path(project_root).resolve()
        document = workflow_path
        workflow_source = "state://approved-workflow"
    else:
        workflow_path = Path(workflow_path).resolve()
        root = Path(project_root).resolve() if project_root else workflow_path.parents[3]
        document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        workflow_source = str(workflow_path.relative_to(root)).replace("\\", "/")
    if not isinstance(document, dict):
        raise WorkflowValidationError("workflow YAML root must be a mapping")
    if str(document.get("schema_version")) not in SUPPORTED_SCHEMA_VERSIONS:
        raise WorkflowValidationError(
            f"unsupported schema_version: {document.get('schema_version')!r}"
        )

    workflow = document.get("workflow")
    if not isinstance(workflow, dict):
        raise WorkflowValidationError("missing workflow mapping")
    if workflow.get("status") != "ready":
        raise WorkflowValidationError("only status=ready can be converted")
    if workflow.get("unresolved_inputs"):
        raise WorkflowValidationError("unresolved_inputs is not empty")

    execution_mode = workflow.get("execution_mode", "static")
    if execution_mode not in {"static", "adaptive"}:
        raise WorkflowValidationError(
            f"unsupported workflow execution_mode: {execution_mode!r}"
        )
    if execution_mode == "static" and selected_tool is not None:
        raise WorkflowValidationError(
            "selected_tool generation is only available for adaptive workflows"
        )

    inputs = workflow.get("inputs") or {}
    provenance = workflow.get("input_provenance") or {}
    for name, schema in (workflow.get("input_schema") or {}).items():
        if schema.get("required") and (
            name not in inputs
            or inputs[name] is None
            or not provenance.get(name, {}).get("confirmed")
        ):
            raise WorkflowValidationError(f"required input is not confirmed: {name}")

    refs = {
        **{f"workflow.inputs.{name}": repr(value) for name, value in inputs.items()},
        **{
            f"workflow.context.{name}": repr(value)
            for name, value in (workflow.get("context") or {}).items()
        },
    }
    unavailable_refs: set[str] = set()
    used: set[str] = set()
    executed_tool_ids = executed_tool_ids or set()
    runtime_decisions = runtime_decisions or {}
    call_cells: list[dict[str, Any]] = []
    reached_adaptive_frontier = False
    pending_conditional_tool_id: str | None = None
    seen_steps: set[str] = set()
    step_orders: set[int] = set()

    steps = sorted(workflow.get("steps") or [], key=lambda item: item.get("order", 0))
    for step in steps:
        step_id, step_order = step.get("id"), step.get("order")
        if not isinstance(step_id, str) or step_id in seen_steps:
            raise WorkflowValidationError(f"missing or duplicate step id: {step_id!r}")
        if (
            not isinstance(step_order, int)
            or step_order <= 0
            or step_order in step_orders
        ):
            raise WorkflowValidationError(f"invalid or duplicate step order: {step_order!r}")
        if (
            step.get("execution", "always") != "always"
            and execution_mode == "static"
        ):
            raise WorkflowValidationError(f"conditional step is unsupported: {step_id}")
        missing = set(step.get("depends_on") or []) - seen_steps
        if missing:
            raise WorkflowValidationError(f"{step_id}: missing dependencies {sorted(missing)}")
        seen_steps.add(step_id)
        step_orders.add(step_order)

        if (
            execution_mode == "adaptive"
            and selected_tool is None
            and step.get("execution", "always") == "conditional"
        ):
            reached_adaptive_frontier = True

        tool_ids: set[str] = set()
        tool_orders: set[int] = set()
        tools = sorted(step.get("tools") or [], key=lambda item: item.get("order", 0))
        for tool in tools:
            tool_id, tool_order = tool.get("id"), tool.get("order")
            function_name = tool.get("tool")
            if not isinstance(tool_id, str) or tool_id in tool_ids:
                raise WorkflowValidationError(f"{step_id}: duplicate tool id {tool_id!r}")
            if (
                not isinstance(tool_order, int)
                or tool_order <= 0
                or tool_order in tool_orders
            ):
                raise WorkflowValidationError(f"{step_id}.{tool_id}: invalid tool order")
            if (
                tool.get("execution", "always") != "always"
                and execution_mode == "static"
            ):
                raise WorkflowValidationError(
                    f"conditional tool is unsupported: {step_id}.{tool_id}"
                )
            tool_ids.add(tool_id)
            tool_orders.add(tool_order)

            tool_execution = tool.get("execution", "always")
            decision = runtime_decisions.get(str(tool_id))
            if decision not in {None, "include", "exclude"}:
                raise WorkflowValidationError(
                    f"invalid runtime decision for {tool_id}: {decision!r}"
                )
            if (
                execution_mode == "adaptive"
                and selected_tool is None
                and tool_execution == "conditional"
                and decision is None
            ):
                reached_adaptive_frontier = True
                if pending_conditional_tool_id is None:
                    pending_conditional_tool_id = str(tool_id)

            source_value = tool.get("tool_source")
            if not isinstance(source_value, str) or not isinstance(function_name, str):
                raise WorkflowValidationError(f"{step_id}.{tool_id}: invalid tool metadata")
            source_path = (root / source_value).resolve()
            try:
                source_path.relative_to(root)
            except ValueError as exc:
                raise WorkflowValidationError(
                    f"tool source is outside project root: {source_value}"
                ) from exc
            source = _function_source(source_path, function_name)
            arguments = tool.get("arguments") or {}
            argument_sources = tool.get("argument_sources") or {}
            if set(arguments) != set(argument_sources):
                raise WorkflowValidationError(
                    f"{step_id}.{tool_id}: arguments and argument_sources differ"
                )
            rendered = [
                f"    {name}={_argument(value, argument_sources[name], refs, f'{step_id}.{tool_id}.{name}', unavailable_refs)},"
                for name, value in arguments.items()
            ]
            passthrough_data = None
            if (
                decision == "exclude"
                and "data" in arguments
                and argument_sources.get("data")
                in {"workflow_input", "context", "step_output"}
            ):
                passthrough_data = _reference(
                    arguments["data"],
                    refs,
                    f"{step_id}.{tool_id}.data",
                    unavailable_refs,
                )

            returns = tool.get("returns") or {}
            result = _identifier(
                returns.get("result_variable"),
                f"{step_id}.{tool_id}.result_variable",
                used,
            )
            lines = [
                f"{result} = {function_name}(",
                *rendered,
                ")",
            ]
            lines.extend(_result_print_lines(function_name, result))
            for output_name, output in (returns.get("outputs") or {}).items():
                output_variable = output.get("variable")
                selector = output.get("selector")
                reference_key = (
                    f"steps.{step_id}.tools.{tool_id}.outputs.{output_name}"
                )

                if (
                    decision == "exclude"
                    and output_name in PASSTHROUGH_DATA_OUTPUTS
                    and passthrough_data is not None
                ):
                    refs[reference_key] = passthrough_data
                    continue

                # 함수 전체 반환값을 같은 변수에 다시 넣는 코드는 생성하지 않는다.
                if selector == "$" and output_variable == result:
                    refs[reference_key] = result
                    if decision == "exclude":
                        unavailable_refs.add(reference_key)
                    continue

                variable = _identifier(
                    output_variable,
                    f"{step_id}.{tool_id}.{output_name}.variable",
                    used,
                )
                if selector == "$":
                    expression = result
                elif isinstance(selector, str) and SELECTOR_RE.fullmatch(selector):
                    expression = result + selector
                else:
                    raise WorkflowValidationError(f"unsupported selector: {selector!r}")
                lines.append(f"{variable} = {expression}")
                refs[reference_key] = variable
                if decision == "exclude":
                    unavailable_refs.add(reference_key)
            should_emit_cell = (
                selected_tool == (step_id, tool_id)
                if selected_tool is not None
                else (
                    not reached_adaptive_frontier
                    and str(tool_id) not in executed_tool_ids
                    and decision != "exclude"
                )
            )
            if should_emit_cell:
                call_cells.append(
                    _cell(
                        source + "\n" + "\n".join(lines) + "\n",
                        skill=step.get("skill"),
                        role="tool_execution",
                        cell_id=f"{step_id}-{tool_id}",
                        step_id=step_id,
                        tool_id=tool_id,
                        tool=function_name,
                    )
                )

        for output_name, value in (step.get("outputs") or {}).items():
            step_reference_key = f"steps.{step_id}.outputs.{output_name}"
            refs[step_reference_key] = _reference(
                value, refs, f"{step_id}.outputs.{output_name}"
            )
            match = REF_RE.fullmatch(value) if isinstance(value, str) else None
            if match and match.group(1) in unavailable_refs:
                unavailable_refs.add(step_reference_key)

    cells = list(call_cells)
    if selected_tool is None and (
        execution_mode == "static"
        or (
            execution_mode == "adaptive"
            and pending_conditional_tool_id is None
        )
    ):
        if execution_mode == "adaptive" and "exclude" in runtime_decisions.values():
            final_lines = [
                f"adaptive_runtime_decisions = {runtime_decisions!r}",
                "adaptive_runtime_decisions",
            ]
        else:
            final_lines = ["workflow_outputs = {"]
            for name, value in (workflow.get("outputs") or {}).items():
                final_lines.append(
                    f"    {name!r}: "
                    f"{_reference(value, refs, f'workflow.outputs.{name}', unavailable_refs)},"
                )
            final_lines.extend(["}", "workflow_outputs"])
        cells.append(
            _cell(
                "\n".join(final_lines) + "\n",
                skill=None,
                role="workflow_outputs",
                cell_id="workflow-outputs",
                step_id=None,
                tool_id=None,
                tool=None,
            )
        )
    elif len(cells) != 1:
        if selected_tool is not None:
            raise WorkflowValidationError(
                f"selected adaptive tool was not found: {selected_tool!r}"
            )
        if not cells and not allow_empty_adaptive_segment:
            raise WorkflowValidationError(
                "adaptive workflow has no executable Tool before its first "
                "unresolved conditional Tool"
            )
    for order, cell in enumerate(cells, start=1):
        cell["order"] = order

    return {
        "schema_version": "1.0",
        "artifact_type": "notebook_execution_cell_set",
        "workflow": {
            "id": workflow.get("id"),
            "job_id": job_id,
            "execution_mode": execution_mode,
            "source": workflow_source,
            "pending_conditional_tool_id": pending_conditional_tool_id,
        },
        "cells": cells,
    }


def generate_cell_json_files(
    workflow_path: str | Path | dict[str, Any],
    output_dir: str | Path,
    project_root: str | Path | None = None,
    job_id: str = "",
) -> list[Path]:
    """Static 전체 셀 또는 adaptive 최초 실행 구간을 파일로 저장한다."""
    artifact = build_notebook(workflow_path, project_root, job_id)
    return write_cell_code_files(artifact, output_dir)


def write_cell_code_files(
    artifact: dict[str, Any],
    output_dir: str | Path,
) -> list[Path]:
    """이미 생성된 Notebook artifact의 각 셀을 JSON 파일로 저장한다."""
    output_dir = Path(output_dir).resolve()
    if output_dir.exists() and not output_dir.is_dir():
        raise WorkflowValidationError(f"output path is not a directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for cell in artifact["cells"]:
        safe_id = re.sub(r"[^0-9A-Za-z_-]+", "_", cell["id"]).strip("_")
        safe_id = safe_id[:80].rstrip("_-")
        path = output_dir / f"{cell['order']:03d}_{safe_id}.py"
        path.write_text(cell["code"].rstrip() + "\n", encoding="utf-8")
        written.append(path)
    return written


def write_cell_json_files(
    artifact: dict[str, Any],
    output_dir: str | Path,
) -> list[Path]:
    return write_cell_code_files(artifact, output_dir)


def generate_next_cell_json(
    workflow_path: str | Path,
    output_path: str | Path,
    *,
    step_id: str,
    tool_id: str,
    order: int,
    project_root: str | Path | None = None,
    job_id: str = "",
) -> Path:
    """Generate exactly one selected cell for an adaptive workflow."""
    if order <= 0:
        raise WorkflowValidationError("adaptive cell order must be positive")
    artifact = build_notebook(
        workflow_path,
        project_root,
        job_id,
        selected_tool=(step_id, tool_id),
    )
    cell = artifact["cells"][0]
    cell["order"] = order
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(cell["code"].rstrip() + "\n", encoding="utf-8")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", type=Path)
    parser.add_argument(
        "-o", "--output-dir", type=Path,
        help="셀별 JSON 파일을 저장할 디렉터리",
    )
    parser.add_argument("--output-file", type=Path)
    parser.add_argument("--step-id")
    parser.add_argument("--tool-id")
    parser.add_argument("--order", type=int)
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument(
        "--job-id", "--run-id", dest="job_id", default="",
        help="대화 시작 시 부여받은 실행 식별자",
    )
    args = parser.parse_args()

    adaptive_values = (
        args.output_file,
        args.step_id,
        args.tool_id,
        args.order,
    )
    if any(value is not None for value in adaptive_values):
        if not all(value is not None for value in adaptive_values):
            parser.error(
                "adaptive cell generation requires --output-file, --step-id, "
                "--tool-id and --order"
            )
        print(
            generate_next_cell_json(
                args.workflow,
                args.output_file,
                step_id=args.step_id,
                tool_id=args.tool_id,
                order=args.order,
                project_root=args.project_root,
                job_id=args.job_id,
            )
        )
        return

    if args.output_dir is None:
        parser.error("workflow cell generation requires --output-dir")
    for path in generate_cell_json_files(
        args.workflow, args.output_dir, args.project_root, args.job_id
    ):
        print(path)


if __name__ == "__main__":
    main()
