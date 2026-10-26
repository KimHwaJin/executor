"""Deterministic analysis snapshots from confirmed graph state, never inferred facts."""
from __future__ import annotations

from copy import deepcopy
import inspect
import re
from typing import Any, TypedDict


class AnalysisRecord(TypedDict, total=False):
    task_id: str
    original_request: str
    workflow: dict[str, Any]
    confirmed_inputs: list[dict[str, Any]]
    confirmed_additional_information: dict[str, Any]
    confirmed_workflow_inputs: dict[str, Any]
    summary_kind: str
    tool_inputs_confirmed: bool
    planned_tools: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    execution_status: str
    report_status: str
    result_summary: str
    limitations: list[str]
    references: dict[str, Any]


def excerpt(value: str, limit: int = 4000) -> str:
    return value if len(value) <= limit else value[:limit] + "\n[이후 내용 생략; 원본 참조 필요]"


def compact_evidence(value: Any, limit: int = 4000) -> Any:
    """Exclude binary outputs and bound nested notebook evidence, with visible omissions."""
    if isinstance(value, str):
        return excerpt(value, limit)
    if isinstance(value, list):
        items = [compact_evidence(item, limit) for item in value[:20]]
        if len(value) > 20:
            items.append({"omitted_items": len(value) - 20})
        return items
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:40]:
            result[key] = "[binary omitted]" if str(key).startswith("image/") else compact_evidence(item, limit)
        if len(value) > 40:
            result["omitted_fields"] = len(value) - 40
        return result
    return value


def snapshot_analysis(state: dict) -> dict:
    task_id = state.get("task_id")
    if not task_id:
        return {}
    records = dict(state.get("analysis_records") or {})
    record = deepcopy(records.get(task_id) or {})
    prior_request = ((state.get("workflow_definition") or {}).get("workflow") or {}).get("user_request")
    record.setdefault("original_request", prior_request or state.get("user_request", ""))
    record["task_id"] = task_id
    definition = state.get("workflow_definition") or state.get("workflow") or {}
    workflow = definition.get("workflow") or {}
    compiled = (state.get("workflow") or {}).get("workflow") or {}
    steps = compiled.get("steps") or definition.get("steps") or workflow.get("steps") or []
    # Compiled workflows and Workflow 1.0 definitions both expose skills/tools by step.
    planned = []
    for si, step in enumerate(steps):
        for ti, tool in enumerate(step.get("tools") or []):
            planned.append({"step_id": step.get("step_id") or step.get("id") or f"step_{si}",
                            "skill": step.get("skill"),
                            "tool_id": tool.get("tool_id") or tool.get("id") or f"step_{si}_tool_{ti}",
                            "tool": tool.get("tool") or tool.get("name"),
                            "condition": tool.get("condition"),
                            "execution": tool.get("execution"),
                            "runtime_decision": (state.get("adaptive_runtime_decisions") or {}).get(tool.get("id"))})
    if definition:
        record["workflow"] = {"id": state.get("workflow_id") or workflow.get("id"),
                              "version_id": state.get("workflow_version_id"),
                              "revision": state.get("workflow_content_revision"),
                              "name": workflow.get("name"),
                              "approved": bool((state.get("approval") or {}).get("approved"))}
        record["planned_tools"] = planned
    confirmed = bool(state.get("tool_inputs_confirmed") or (state.get("approval") or {}).get("approved"))
    record["tool_inputs_confirmed"] = confirmed
    if not confirmed:
        for key in ("confirmed_inputs", "confirmed_additional_information", "confirmed_workflow_inputs"):
            record.pop(key, None)
    if confirmed:
        record["confirmed_inputs"] = deepcopy(state.get("execution_inputs") or [])
        # Legacy workflows keep confirmed answers in additional_information.
        record["confirmed_additional_information"] = deepcopy(state.get("additional_information") or {})
        record["tool_inputs_confirmed"] = True
        record["confirmed_workflow_inputs"] = deepcopy(compiled.get("inputs") or {})
    history = state.get("executor_result_history") or []
    if history:
        record["tool_results"] = [compact_evidence(item) for item in history]
    record["execution_status"] = state.get("execution_status") or "not_started"
    record["report_status"] = state.get("report_status") or "not_generated"
    report = state.get("analysis_report") or {}
    if report.get("content"):
        record["result_summary"] = excerpt(report["content"])
        record["summary_kind"] = "report_excerpt"
    record["limitations"] = [str(state["result_collection_error"])] if state.get("result_collection_error") else []
    # Preserve explicitly labelled limitations even when the report excerpt ends
    # before the final sections. These are verbatim evidence, not inferred facts.
    content = report.get("content") or ""
    sections = re.split(r"(?m)(?=^#{1,6} )", content)
    for section in sections:
        heading = section.split("\n", 1)[0]
        if re.search(r"한계|제약|주의|limitations|caveats", heading, re.I):
            record["limitations"].append(excerpt(section, 2000))
    report_message_id = f"report-{task_id}"
    if report.get("content"):
        for message in reversed(state.get("messages") or []):
            item = message if isinstance(message, dict) else message.model_dump()
            if item.get("content") == report["content"] and item.get("id"):
                report_message_id = item["id"]
                break
    record["references"] = {
        "task_id": task_id,
        "execution_id": state.get("execution_id"),
        "artifact_files": deepcopy(state.get("artifact_files") or {}),
        "report_message_id": report_message_id if report.get("content") else None,
        "report_artifact_response": compact_evidence(state.get("report_artifact_response") or {}),
    }
    records[task_id] = record
    return {"analysis_records": records}


def record_after(node):
    """Persist a snapshot in the same node update as the authoritative changes.

    Interrupted nodes do not record until their resume has completed successfully.
    """
    takes_config = "config" in inspect.signature(node).parameters
    async def recorded(state, config):
        update = node(state, config) if takes_config else node(state)
        if inspect.isawaitable(update):
            update = await update
        update = update or {}
        return {**update, **snapshot_analysis({**state, **update})}
    return recorded
