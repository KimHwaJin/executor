"""Schema and parser for Markdown Skill documents."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Schema contract: allowed Markdown sections and their exact order.
SECTION_ORDER = (
    "capabilities",
    "limitations",
    "when_to_use",
    "not_for",
    "typical_previous_skill",
    "typical_next_skills",
    "Tool list",
)


# Schema models: the normalized representation of one Skill document.
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillToolExecution(StrEnum):
    ALWAYS = "always"
    CONDITIONAL = "conditional"


class SkillTool(StrictModel):
    name: str = Field(min_length=1)
    execution: SkillToolExecution
    condition: str = Field(min_length=1)
    condition_tool: str = Field(min_length=1)
    role: str = Field(min_length=1)


class SkillDefinition(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1)
    title: str = Field(min_length=1)
    capabilities: list[str] = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    when_to_use: list[str] = Field(min_length=1)
    not_for: list[str] = Field(min_length=1)
    typical_previous_skills: list[str]
    typical_next_skills: list[str]
    tools: list[SkillTool] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_tools(self) -> "SkillDefinition":
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("Skill tool names must be unique.")

        seen: set[str] = set()
        known_skills = set(self.typical_previous_skills) | {self.name}
        for tool in self.tools:
            condition_tool = tool.condition_tool.strip("`")
            if tool.execution == SkillToolExecution.ALWAYS and condition_tool != "없음":
                raise ValueError("항상 실행 Tool의 condition_tool은 없음이어야 합니다.")
            if condition_tool == "없음":
                seen.add(tool.name)
                continue
            if "." in condition_tool:
                skill_name, _, tool_name = condition_tool.partition(".")
                if not skill_name or not tool_name:
                    raise ValueError(f"잘못된 condition_tool 형식입니다: {condition_tool!r}")
                if skill_name not in known_skills:
                    raise ValueError(
                        "외부 condition_tool은 현재 Skill 또는 typical_previous_skill을 "
                        f"참조해야 합니다: {condition_tool!r}"
                    )
            elif condition_tool not in seen:
                raise ValueError(
                    "내부 condition_tool은 같은 Skill의 앞선 Tool이어야 합니다: "
                    f"{condition_tool!r}"
                )
            seen.add(tool.name)
        return self


# Parser and validation implementation starts here.
def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = re.match(r"^\ufeff?---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
    if match is None:
        raise ValueError("A skill must start with name/description frontmatter.")
    values: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator or not key.strip() or not value.strip():
            raise ValueError(f"Invalid frontmatter line: {line!r}")
        values[key.strip()] = value.strip()
    if set(values) != {"name", "description"}:
        raise ValueError("Frontmatter must contain only name and description.")
    return values, text[match.end():]


def _bullet_items(section: str) -> list[str]:
    return [
        line.strip()[2:].strip().strip("`")
        for line in section.splitlines()
        if line.strip().startswith("- ")
    ]


def _skill_links(section: str) -> list[str]:
    items = _bullet_items(section)
    if items == ["\uc5c6\uc74c"]:
        return []
    return items


def _parse_tools(section: str) -> list[SkillTool]:
    tools: list[SkillTool] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        columns = [column.strip() for column in stripped.strip("|").split("|")]
        if len(columns) != 5:
            continue
        if columns[0] == "Tool" or set(columns[0]) == {"-"}:
            continue
        execution_map = {
            "\ud56d\uc0c1 \uc2e4\ud589": SkillToolExecution.ALWAYS,
            "\uc870\uac74\ubd80 \uc2e4\ud589": SkillToolExecution.CONDITIONAL,
        }
        if columns[1] not in execution_map:
            raise ValueError(f"Unsupported tool execution mode: {columns[1]!r}")
        tools.append(
            SkillTool(
                name=columns[0].strip("`"),
                execution=execution_map[columns[1]],
                condition=columns[2],
                condition_tool=columns[3].strip("`"),
                role=columns[4],
            )
        )
    if not tools:
        raise ValueError("The Tool list must contain at least one tool.")
    return tools


def parse_skill_markdown(text: str) -> SkillDefinition:
    """Parse and validate one Skill Markdown document."""
    frontmatter, body = _parse_frontmatter(text)
    title_match = re.search(r"^#\s+(.+?)\s*$", body, flags=re.MULTILINE)
    if title_match is None:
        raise ValueError("A skill title is required.")

    headings = list(re.finditer(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE))
    heading_names = tuple(match.group(1) for match in headings)
    if heading_names != SECTION_ORDER:
        raise ValueError(
            f"Skill section order does not match the schema: {heading_names!r}"
        )

    sections: dict[str, str] = {}
    for index, heading in enumerate(headings):
        start = heading.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        sections[heading.group(1)] = body[start:end]

    return SkillDefinition(
        name=frontmatter["name"],
        description=frontmatter["description"],
        title=title_match.group(1),
        capabilities=_bullet_items(sections["capabilities"]),
        limitations=_bullet_items(sections["limitations"]),
        when_to_use=_bullet_items(sections["when_to_use"]),
        not_for=_bullet_items(sections["not_for"]),
        typical_previous_skills=_skill_links(sections["typical_previous_skill"]),
        typical_next_skills=_skill_links(sections["typical_next_skills"]),
        tools=_parse_tools(sections["Tool list"]),
    )


def load_skill(path: str | Path) -> SkillDefinition:
    path = Path(path)
    return parse_skill_markdown(path.read_text(encoding="utf-8"))


__all__ = [
    "SECTION_ORDER",
    "SkillDefinition",
    "SkillTool",
    "SkillToolExecution",
    "load_skill",
    "parse_skill_markdown",
]
