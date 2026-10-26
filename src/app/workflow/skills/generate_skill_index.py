"""Generate skill_index.yaml from structured Skill Markdown files."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml


SECTION_NAMES = (
    "capabilities",
    "limitations",
    "when_to_use",
    "not_for",
    "typical_previous_skill",
    "typical_next_skills",
    "Tool list",
)
EXECUTION_VALUES = {
    "항상 실행": "always",
    "조건부 실행": "conditional",
}


def _frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    match = re.match(r"^\ufeff?---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
    if match is None:
        raise ValueError(f"{path}: frontmatter가 없습니다.")
    values = yaml.safe_load(match.group(1))
    if not isinstance(values, dict) or set(values) != {"name", "description"}:
        raise ValueError(f"{path}: frontmatter에는 name과 description만 필요합니다.")
    return values, text[match.end():]


def _sections(body: str, path: Path) -> dict[str, str]:
    headings = list(re.finditer(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE))
    names = tuple(match.group(1) for match in headings)
    if names != SECTION_NAMES:
        raise ValueError(f"{path}: Skill 섹션 이름 또는 순서가 다릅니다: {names!r}")
    sections: dict[str, str] = {}
    for index, heading in enumerate(headings):
        start = heading.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        sections[heading.group(1)] = body[start:end]
    return sections


def _bullets(section: str) -> list[str]:
    return [
        line.strip()[2:].strip().strip("`")
        for line in section.splitlines()
        if line.strip().startswith("- ")
    ]


def _skill_links(section: str) -> list[str]:
    values = _bullets(section)
    return [] if values == ["없음"] else values


def _tools(section: str, path: Path) -> list[dict[str, str]]:
    tools: list[dict[str, str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        columns = [value.strip() for value in stripped.strip("|").split("|")]
        if len(columns) != 5:
            raise ValueError(f"{path}: Tool 표는 5개 컬럼이어야 합니다: {line!r}")
        if columns[0] == "Tool" or set(columns[0]) == {"-"}:
            continue
        execution = EXECUTION_VALUES.get(columns[1])
        if execution is None:
            raise ValueError(f"{path}: 알 수 없는 Tool 실행 방식입니다: {columns[1]!r}")
        tools.append(
            {
                "tool": columns[0].strip("`"),
                "execution": execution,
                "condition": columns[2],
                "condition_tool": columns[3].strip("`"),
                "role": columns[4],
            }
        )
    if not tools:
        raise ValueError(f"{path}: Tool 목록이 비어 있습니다.")
    return tools



def _validate_condition_tools(name: str, definition: dict[str, Any], path: Path) -> None:
    previous_tools: set[str] = set()
    allowed_external_skills = {name, *definition["typical_previous_skills"]}
    for tool in definition["tools"]:
        condition_tool = tool["condition_tool"]
        if tool["execution"] == "always" and condition_tool != "없음":
            raise ValueError(f"{path}: always tools must use condition_tool 없음: {tool['tool']!r}")
        if condition_tool == "없음":
            previous_tools.add(tool["tool"])
            continue
        if "." in condition_tool:
            skill_name, _, tool_name = condition_tool.partition(".")
            if not skill_name or not tool_name:
                raise ValueError(f"{path}: invalid condition_tool: {condition_tool!r}")
            if skill_name not in allowed_external_skills:
                raise ValueError(
                    f"{path}: external condition_tool must reference the current skill "
                    f"or typical_previous_skill: {condition_tool!r}"
                )
        elif condition_tool not in previous_tools:
            raise ValueError(
                f"{path}: internal condition_tool must reference a previous tool: "
                f"{condition_tool!r}"
            )
        previous_tools.add(tool["tool"])
def _skill(path: Path, root: Path) -> tuple[str, dict[str, Any]]:
    frontmatter, body = _frontmatter(path.read_text(encoding="utf-8"), path)
    sections = _sections(body, path)
    name = str(frontmatter["name"])
    definition = {
        "source": path.relative_to(root).as_posix(),
        "description": str(frontmatter["description"]),
        "capabilities": _bullets(sections["capabilities"]),
        "limitations": _bullets(sections["limitations"]),
        "when_to_use": _bullets(sections["when_to_use"]),
        "not_for": _bullets(sections["not_for"]),
        "typical_previous_skills": _skill_links(
            sections["typical_previous_skill"]
        ),
        "typical_next_skills": _skill_links(sections["typical_next_skills"]),
        "tools": _tools(sections["Tool list"], path),
    }
    _validate_condition_tools(name, definition, path)
    return name, definition


def build_index(root: Path) -> dict[str, Any]:
    skills: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*.md")):
        if {"tmp", "past"}.intersection(path.relative_to(root).parts):
            continue
        name, definition = _skill(path, root)
        if name in skills:
            raise ValueError(f"중복 Skill ID입니다: {name!r}")
        skills[name] = definition

    known = set(skills)
    for name, definition in skills.items():
        references = set(definition["typical_previous_skills"]) | set(
            definition["typical_next_skills"]
        )
        unknown = references - known
        if unknown:
            raise ValueError(f"{name}: 존재하지 않는 Skill 참조입니다: {sorted(unknown)}")

    return {
        "schema_version": "2.0",
        "index_type": "skill_index",
        "description": "Skill Markdown에서 규칙 기반으로 생성한 Workflow 탐색용 색인이다.",
        "skills": skills,
    }


def write_index(index: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        yaml.safe_dump(
            index,
            allow_unicode=True,
            sort_keys=False,
            width=100,
        ),
        encoding="utf-8",
    )
    temporary.replace(output)


def main() -> None:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skills-dir", type=Path, default=root)
    parser.add_argument("--output", type=Path, default=root / "skill_index.yaml")
    arguments = parser.parse_args()
    index = build_index(arguments.skills_dir.resolve())
    write_index(index, arguments.output.resolve())
    print(f"generated {len(index['skills'])} skills: {arguments.output.resolve()}")


if __name__ == "__main__":
    main()

