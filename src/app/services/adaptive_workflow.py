"""Resolve Skill condition Tool relationships for adaptive execution."""

from __future__ import annotations

from typing import Any

from app.agents.workflow_generator.workflow_generator_tools import (
    _load_skill_index_document,
)


def _ordered_tools(workflow_document: dict[str, Any]) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    workflow = workflow_document.get("workflow") or {}
    for step in sorted(workflow.get("steps") or [], key=lambda item: item["order"]):
        for tool in sorted(step.get("tools") or [], key=lambda item: item["order"]):
            ordered.append(
                {
                    "step_id": step["id"],
                    "skill": step["skill"],
                    "tool_id": tool["id"],
                    "tool": tool["tool"],
                    "execution": tool.get("execution", "always"),
                }
            )
    return ordered


def conditional_candidate(
    workflow_document: dict[str, Any],
    candidate_tool_id: str,
) -> dict[str, Any]:
    """Return one conditional Tool and its concrete preceding condition Tool."""
    ordered = _ordered_tools(workflow_document)
    candidate_index = next(
        (
            index
            for index, item in enumerate(ordered)
            if item["tool_id"] == candidate_tool_id
        ),
        None,
    )
    if candidate_index is None:
        raise ValueError(f"unknown conditional Tool id: {candidate_tool_id}")
    candidate = ordered[candidate_index]
    if candidate["execution"] != "conditional":
        raise ValueError(f"Tool is not conditional: {candidate_tool_id}")

    skills = _load_skill_index_document().get("skills") or {}
    rules = {
        str(item.get("tool")): item
        for item in (skills.get(candidate["skill"]) or {}).get("tools") or []
        if isinstance(item, dict) and item.get("tool")
    }
    rule = rules.get(candidate["tool"])
    if not isinstance(rule, dict):
        raise ValueError(
            f"Skill condition rule not found: {candidate['skill']}.{candidate['tool']}"
        )
    condition_ref = str(rule.get("condition_tool") or "없음")
    if condition_ref == "없음":
        raise ValueError(f"conditional Tool has no condition_tool: {candidate_tool_id}")
    if "." in condition_ref:
        condition_skill, condition_tool = condition_ref.split(".", 1)
    else:
        condition_skill, condition_tool = candidate["skill"], condition_ref

    matches = [
        item
        for item in ordered[:candidate_index]
        if item["skill"] == condition_skill and item["tool"] == condition_tool
    ]
    if not matches:
        raise ValueError(
            f"condition Tool is missing before {candidate_tool_id}: {condition_ref}"
        )
    matched = matches[-1]
    return {
        "tool_id": candidate_tool_id,
        "tool": candidate["tool"],
        "condition": str(rule.get("condition") or ""),
        "condition_tool_id": matched["tool_id"],
        "condition_tool": matched["tool"],
    }


def decision_payload(
    workflow_document: dict[str, Any],
    candidate_tool_id: str,
    observations: dict[str, Any],
) -> dict[str, Any]:
    candidate = conditional_candidate(workflow_document, candidate_tool_id)
    condition_tool_id = candidate["condition_tool_id"]
    if condition_tool_id not in observations:
        raise ValueError(
            f"condition Tool result is missing: {condition_tool_id}"
        )
    condition_result = observations[condition_tool_id]
    if (
        isinstance(condition_result, dict)
        and condition_result.get("status")
        and condition_result.get("status") != "SUCCEEDED"
    ):
        raise ValueError(
            "condition Tool did not complete successfully: "
            f"{condition_tool_id} ({condition_result.get('status')})"
        )
    return {
        "candidates": [
            {
                **candidate,
                "condition_tool_result": condition_result,
            }
        ]
    }


__all__ = ["conditional_candidate", "decision_payload"]
