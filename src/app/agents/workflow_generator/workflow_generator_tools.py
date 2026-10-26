"""Read only the Workflow resources needed by the generator agent."""

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from langchain.tools import tool


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = (PROJECT_ROOT / "app" / "workflow" / "skills").resolve()
SKILL_INDEX_PATH = SKILLS_ROOT / "skill_index.yaml"
TOOL_REGISTRY_PATH = (
    PROJECT_ROOT / "app" / "workflow" / "tools" / "tool_registry.yaml"
).resolve()
MAX_FILE_SIZE = 200_000
MAX_SKILL_DOCUMENTS = 20


def _read_text(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"파일을 찾을 수 없습니다: {path}")
    if path.stat().st_size > MAX_FILE_SIZE:
        raise ValueError(f"파일 크기가 {MAX_FILE_SIZE} byte를 초과합니다: {path}")
    return path.read_text(encoding="utf-8")


def _replace_exact_reference(node: Any, old: str, new: str) -> None:
    """Replace one exact Workflow reference throughout a JSON-like document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if value == old:
                node[key] = new
            else:
                _replace_exact_reference(value, old, new)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            if value == old:
                node[index] = new
            else:
                _replace_exact_reference(value, old, new)


@lru_cache(maxsize=1)
def _load_skill_index_document() -> dict[str, Any]:
    return yaml.safe_load(_read_text(SKILL_INDEX_PATH)) or {}


@lru_cache(maxsize=1)
def _load_tool_registry_document() -> dict[str, Any]:
    return yaml.safe_load(_read_text(TOOL_REGISTRY_PATH)) or {}


@lru_cache(maxsize=1)
def _load_skill_sources() -> dict[str, str]:
    skills = _load_skill_index_document().get("skills") or {}
    return {
        str(skill_name): str(skill_definition["source"])
        for skill_name, skill_definition in skills.items()
    }


def load_workflow_catalog_context() -> str:
    """Return a compact Skill catalog for the first LLM selection call.

    Detailed Skill documents and Tool Registry definitions are deliberately
    omitted here. They are returned only for selected Skills by
    ``read_skill_documents``.
    """
    index = _load_skill_index_document()
    compact_skills: dict[str, dict[str, Any]] = {}
    for skill_name, definition in (index.get("skills") or {}).items():
        compact_skills[str(skill_name)] = {
            key: deepcopy(definition[key])
            for key in ("description", "when_to_use", "not_for")
            if definition.get(key)
        }

    compact_catalog = {
        "index_type": index.get("index_type", "skill_index"),
        "skills": compact_skills,
    }
    return (
        "# 사전 주입 Skill 요약 목록\n\n"
        "아래 목록은 필요한 Skill을 고르기 위한 축약 정보입니다. "
        "Tool 상세 정의는 선택한 Skill 문서를 조회할 때 함께 제공됩니다.\n\n"
        f"```yaml\n{yaml.safe_dump(compact_catalog, allow_unicode=True, sort_keys=False)}```"
    )


def _resolve_skill_document(skill_name: str) -> tuple[str, Path]:
    if not isinstance(skill_name, str) or not skill_name.strip():
        raise ValueError("Skill 이름을 입력해주세요.")

    requested_name = skill_name.strip()
    sources = _load_skill_sources()
    source = sources.get(requested_name)
    if source is None:
        aliases = {
            alias: (name, path)
            for name, path in sources.items()
            for alias in {
                path,
                Path(path).name,
                Path(path).stem,
                f"app/workflow/skills/{path}",
            }
        }
        matched = aliases.get(requested_name)
        if matched is None:
            raise ValueError(
                f"Skill Index에 없는 Skill입니다: {requested_name}. "
                f"사용 가능한 Skill: {', '.join(sorted(sources))}"
            )
        requested_name, source = matched

    resolved_path = (SKILLS_ROOT / source).resolve()
    if not resolved_path.is_relative_to(SKILLS_ROOT):
        raise PermissionError("Skill 디렉터리 내부 문서만 읽을 수 있습니다.")
    if resolved_path.suffix.lower() != ".md":
        raise ValueError("Skill Markdown 문서만 읽을 수 있습니다.")
    return requested_name, resolved_path


def validate_skill_names(skill_names: list[str]) -> list[str]:
    """Validate an LLM-selected list against exact Skill Index names."""
    if not isinstance(skill_names, list) or not skill_names:
        raise ValueError("하나 이상의 Skill 이름이 필요합니다.")
    if len(skill_names) > MAX_SKILL_DOCUMENTS:
        raise ValueError(
            f"한 번에 최대 {MAX_SKILL_DOCUMENTS}개의 Skill을 선택할 수 있습니다."
        )
    if len(skill_names) != len(set(skill_names)):
        raise ValueError("Skill 이름은 중복할 수 없습니다.")

    available_names = set(_load_skill_sources())
    unknown_names = [name for name in skill_names if name not in available_names]
    if unknown_names:
        raise ValueError(
            "Skill Index에 없는 Skill입니다: " + ", ".join(unknown_names)
        )
    return list(skill_names)


def _relevant_tool_definitions(skill_names: list[str]) -> dict[str, Any]:
    skill_definitions = _load_skill_index_document().get("skills") or {}
    registry_tools = _load_tool_registry_document().get("tools") or {}
    selected_tools: dict[str, Any] = {}

    for skill_name in skill_names:
        for item in skill_definitions[skill_name].get("tools") or []:
            tool_name = item.get("tool") if isinstance(item, dict) else item
            if not tool_name or tool_name in selected_tools:
                continue
            if tool_name not in registry_tools:
                raise ValueError(
                    f"Skill '{skill_name}'이 참조하는 Tool이 Registry에 없습니다: "
                    f"{tool_name}"
                )
            selected_tools[str(tool_name)] = deepcopy(registry_tools[tool_name])
    return selected_tools


def canonicalize_registry_tool_sources(document: dict[str, Any]) -> None:
    """Apply authoritative Registry paths and output selectors."""
    registry_tools = _load_tool_registry_document().get("tools") or {}
    workflow = document.get("workflow") or {}
    for step in workflow.get("steps") or []:
        step_id = str(step.get("id") or "")
        for workflow_tool in step.get("tools") or []:
            tool_name = workflow_tool.get("tool")
            tool_id = str(workflow_tool.get("id") or "")
            definition = registry_tools.get(tool_name)
            if not isinstance(definition, dict):
                raise ValueError(f"Tool Registry에 없는 Tool입니다: {tool_name!r}")
            source = definition.get("source")
            if not isinstance(source, str) or not source.strip():
                raise ValueError(
                    f"Tool Registry source가 올바르지 않습니다: {tool_name!r}"
                )
            source_path = (TOOL_REGISTRY_PATH.parent / source).resolve()
            if not source_path.is_relative_to(TOOL_REGISTRY_PATH.parent):
                raise PermissionError(
                    f"Tool Registry 밖의 source입니다: {tool_name!r}"
                )
            if source_path.suffix.lower() != ".py" or not source_path.is_file():
                raise FileNotFoundError(
                    f"Tool Registry source 파일을 찾을 수 없습니다: {source_path}"
                )
            workflow_tool["tool_source"] = source_path.relative_to(
                PROJECT_ROOT
            ).as_posix()

            registry_outputs = (
                (definition.get("returns") or {}).get("outputs") or {}
            )
            workflow_outputs = (
                (workflow_tool.get("returns") or {}).get("outputs") or {}
            )
            if (
                len(registry_outputs) == 1
                and len(workflow_outputs) == 1
                and next(iter(workflow_outputs)) not in registry_outputs
            ):
                old_output_name, workflow_output = next(
                    iter(workflow_outputs.items())
                )
                canonical_output_name = next(iter(registry_outputs))
                workflow_outputs.clear()
                workflow_outputs[canonical_output_name] = workflow_output
                old_reference = (
                    f"${{steps.{step_id}.tools.{tool_id}.outputs."
                    f"{old_output_name}}}"
                )
                new_reference = (
                    f"${{steps.{step_id}.tools.{tool_id}.outputs."
                    f"{canonical_output_name}}}"
                )
                _replace_exact_reference(document, old_reference, new_reference)
            for output_name, workflow_output in workflow_outputs.items():
                registry_output = registry_outputs.get(output_name)
                if not isinstance(registry_output, dict):
                    raise ValueError(
                        f"Tool Registry에 없는 return output입니다: "
                        f"{tool_name}.{output_name}"
                    )
                selector = registry_output.get("selector")
                if not isinstance(selector, str) or not selector:
                    raise ValueError(
                        f"Tool Registry selector가 올바르지 않습니다: "
                        f"{tool_name}.{output_name}"
                    )
                if not isinstance(workflow_output, dict):
                    raise ValueError(
                        f"Workflow return output이 올바르지 않습니다: "
                        f"{tool_name}.{output_name}"
                    )
                workflow_output["selector"] = selector


def _skill_tool_rules() -> dict[tuple[str, str], dict[str, Any]]:
    skills = _load_skill_index_document().get("skills") or {}
    rules: dict[tuple[str, str], dict[str, Any]] = {}
    for skill_name, definition in skills.items():
        for item in definition.get("tools") or []:
            if not isinstance(item, dict):
                continue
            tool_name = item.get("tool")
            if tool_name:
                rules[(str(skill_name), str(tool_name))] = item
    return rules



def normalize_skill_condition_contract(document: dict[str, Any]) -> None:
    """Normalize generated Workflow execution flags from Skill condition_tool rules."""
    no_condition_tool = "\uc5c6\uc74c"
    workflow = document.get("workflow") or {}
    rules = _skill_tool_rules()
    has_adaptive_candidate = False

    for step in workflow.get("steps") or []:
        skill_name = str(step.get("skill") or "")
        if not skill_name or skill_name == "data_load":
            continue
        for workflow_tool in step.get("tools") or []:
            if not isinstance(workflow_tool, dict):
                continue
            tool_name = str(workflow_tool.get("tool") or "")
            rule = rules.get((skill_name, tool_name))
            if not rule or str(rule.get("execution") or "") != "conditional":
                continue
            condition_tool = str(rule.get("condition_tool") or no_condition_tool)
            workflow_tool["condition"] = None
            if condition_tool == no_condition_tool:
                workflow_tool["execution"] = "always"
            else:
                workflow_tool["execution"] = "conditional"
                has_adaptive_candidate = True

    if has_adaptive_candidate:
        workflow["execution_mode"] = "adaptive"
    elif workflow.get("execution_mode") == "adaptive":
        has_conditional = any(
            step.get("execution") == "conditional"
            or any(
                isinstance(tool, dict) and tool.get("execution") == "conditional"
                for tool in step.get("tools") or []
            )
            for step in workflow.get("steps") or []
        )
        if not has_conditional:
            workflow["execution_mode"] = "static"
def validate_skill_condition_contract(document: dict[str, Any]) -> None:
    """Enforce Skill tool inclusion and condition_tool rules."""
    no_condition_tool = "\uc5c6\uc74c"
    workflow = document.get("workflow") or {}
    execution_mode = workflow.get("execution_mode", "static")
    rules = _skill_tool_rules()
    violations: list[str] = []

    workflow_tools_by_skill: dict[str, list[dict[str, Any]]] = {}
    workflow_tool_positions: dict[tuple[str, str], list[tuple[int, int]]] = {}
    workflow_tool_id_positions: dict[str, tuple[int, int]] = {}
    for step in workflow.get("steps") or []:
        skill_name = str(step.get("skill") or "")
        step_order = step.get("order")
        for workflow_tool in step.get("tools") or []:
            if not isinstance(workflow_tool, dict):
                continue
            workflow_tools_by_skill.setdefault(skill_name, []).append(workflow_tool)
            tool_name = str(workflow_tool.get("tool") or "")
            tool_order = workflow_tool.get("order")
            if (
                skill_name
                and tool_name
                and isinstance(step_order, int)
                and isinstance(tool_order, int)
            ):
                position = (step_order, tool_order)
                workflow_tool_positions.setdefault(
                    (skill_name, tool_name), []
                ).append(position)
                tool_id = workflow_tool.get("id")
                if isinstance(tool_id, str):
                    workflow_tool_id_positions[tool_id] = position

    for skill_name, workflow_tools in workflow_tools_by_skill.items():
        if not skill_name or skill_name == "data_load":
            continue
        included_tool_names = {
            str(tool.get("tool") or "") for tool in workflow_tools
        }
        required_always_tools = [
            tool_name
            for (rule_skill, tool_name), rule in rules.items()
            if rule_skill == skill_name
            and str(rule.get("execution") or "") == "always"
        ]
        for tool_name in required_always_tools:
            if tool_name not in included_tool_names:
                violations.append(
                    f"{skill_name}.{tool_name} is an always Tool, so using "
                    f"skill {skill_name!r} requires including it"
                )

    for skill_name, workflow_tools in workflow_tools_by_skill.items():
        for workflow_tool in workflow_tools:
            tool_name = str(workflow_tool.get("tool") or "")
            rule = rules.get((skill_name, tool_name))
            if not rule:
                continue
            condition_tool = str(rule.get("condition_tool") or no_condition_tool)
            skill_execution = str(rule.get("execution") or "")
            if skill_execution != "conditional":
                continue
            tool_id = workflow_tool.get("id") or tool_name
            if condition_tool == no_condition_tool:
                if workflow_tool.get("execution") != "always":
                    violations.append(
                        f"{skill_name}.{tool_name} ({tool_id}) has no condition_tool, "
                        "so an included Tool must be finalized as execution=always"
                    )
                continue
            if execution_mode != "adaptive":
                violations.append(
                    f"{skill_name}.{tool_name} ({tool_id}) has condition_tool "
                    f"{condition_tool!r}, so workflow.execution_mode must be adaptive"
                )
            if workflow_tool.get("execution") != "conditional":
                violations.append(
                    f"{skill_name}.{tool_name} ({tool_id}) has condition_tool "
                    f"{condition_tool!r}, so tool.execution must be conditional"
                )
            if workflow_tool.get("condition") is not None:
                violations.append(
                    f"{skill_name}.{tool_name} ({tool_id}) should not include "
                    "tool.condition in the Workflow definition; runtime decisions "
                    "are stored in execution run state"
                )

            if "." in condition_tool:
                condition_skill, condition_tool_name = condition_tool.split(".", 1)
            else:
                condition_skill, condition_tool_name = skill_name, condition_tool
            condition_positions = workflow_tool_positions.get(
                (condition_skill, condition_tool_name), []
            )
            if not condition_positions:
                violations.append(
                    f"{skill_name}.{tool_name} ({tool_id}) requires condition_tool "
                    f"{condition_skill}.{condition_tool_name}, but it is not included "
                    "in the Workflow"
                )
                continue

            candidate_position = workflow_tool_id_positions.get(str(tool_id))
            if candidate_position is not None and not any(
                condition_position < candidate_position
                for condition_position in condition_positions
            ):
                violations.append(
                    f"condition_tool {condition_skill}.{condition_tool_name} must "
                    f"appear before {skill_name}.{tool_name} ({tool_id})"
                )

    if violations:
        raise ValueError("Skill condition_tool contract violated: " + "; ".join(violations))
@tool
def read_skill_documents(skill_names: list[str]) -> dict[str, Any]:
    """선택한 Skill 문서와 관련 Tool Registry 정의를 한 번에 조회합니다.

    축약 Skill 목록에 있는 Skill 이름을 ``skill_names``에 넣으세요. 호출
    결과의 ``skills``에는 Skill 문서가, ``tools``에는 해당 Skill들이 사용하는
    Tool의 상세 정의만 들어 있습니다. Workflow 생성당 정확히 한 번 호출하세요.
    """
    if not isinstance(skill_names, list) or not skill_names:
        raise ValueError("하나 이상의 Skill 이름이 필요합니다.")
    if len(skill_names) > MAX_SKILL_DOCUMENTS:
        raise ValueError(
            f"한 번에 최대 {MAX_SKILL_DOCUMENTS}개의 Skill 문서를 읽을 수 있습니다."
        )
    if len(skill_names) != len(set(skill_names)):
        raise ValueError("Skill 이름은 중복할 수 없습니다.")

    documents: dict[str, dict[str, str]] = {}
    canonical_names: list[str] = []
    for skill_name in skill_names:
        canonical_name, resolved_path = _resolve_skill_document(skill_name)
        if canonical_name in documents:
            raise ValueError(f"동일한 Skill이 중복 선택되었습니다: {canonical_name}")
        canonical_names.append(canonical_name)
        documents[canonical_name] = {
            "source": resolved_path.relative_to(SKILLS_ROOT).as_posix(),
            "content": _read_text(resolved_path),
        }

    return {
        "skills": documents,
        "tools": _relevant_tool_definitions(canonical_names),
    }


__all__ = [
    "canonicalize_registry_tool_sources",
    "validate_skill_condition_contract",
    "load_workflow_catalog_context",
    "normalize_skill_condition_contract",
    "read_skill_documents",
    "validate_skill_names",
]
