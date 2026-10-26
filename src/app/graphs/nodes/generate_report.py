"""Generate an analysis report from executor events."""

from __future__ import annotations

from app.services.async_io_resources import call_async

import asyncio
import json
import hashlib
from datetime import date
from pathlib import Path
from typing import Any

from app.context.builder import context_payload
from agent_config import AgentSettings
from app.agents.orchestration.dependencies import AgentDependencies
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.schemas.agents.report_generator_schema import (
    ExecutionStepResult,
    ReportGenerationRequest,
)
from app.schemas.agents.orchestration_schema import ExecutorArtifactRequestBody
from app.services.demo_artifact_store import (
    build_run_artifact_dir,
    write_demo_json,
)
from app.services.workflow_persistence import effective_workflow_snapshot
from app.services.executor_client import submit_execution_artifact


_REPORT_OUTPUT_TEXT_LIMIT = 8_000


def _truncate_report_text(value: str) -> str:
    """Keep executor evidence useful without sending whole datasets to the LLM."""

    if len(value) <= _REPORT_OUTPUT_TEXT_LIMIT:
        return value
    head_size = 6_000
    tail_size = _REPORT_OUTPUT_TEXT_LIMIT - head_size
    omitted = len(value) - head_size - tail_size
    return (
        value[:head_size]
        + f"\n...[리포트 입력에서 {omitted:,}자 생략]...\n"
        + value[-tail_size:]
    )


def _compact_report_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove bulky/binary notebook output bodies from report-agent input."""

    compact = {key: value for key, value in payload.items() if key != "outputs"}
    outputs = payload.get("outputs")
    if not isinstance(outputs, list):
        return compact

    compact_outputs: list[dict[str, Any]] = []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        item = dict(output)
        text = item.get("text")
        if isinstance(text, str):
            item["text"] = _truncate_report_text(text)

        data = item.get("data")
        if isinstance(data, dict):
            compact_data: dict[str, Any] = {}
            for mime_type, content in data.items():
                if str(mime_type).startswith("image/"):
                    compact_data[mime_type] = "<binary image omitted>"
                elif isinstance(content, str):
                    compact_data[mime_type] = _truncate_report_text(content)
                else:
                    compact_data[mime_type] = content
            item["data"] = compact_data
        compact_outputs.append(item)
    compact["outputs"] = compact_outputs
    return compact


def _safe_path_segment(value: str, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{label} must be a single non-empty path segment")
    return value


def build_report_artifact_request(
    settings: AgentSettings,
    *,
    execution_id: str,
    task_id: str,
    workflow_id: str,
    content: str,
) -> tuple[dict[str, Any], str | None]:
    """Build INLINE or shared-PV PATH REPORT artifact request."""

    staged_path: str | None = None
    if settings.executor_report_source_type == "PATH":
        relative_path = Path(
            "d-test",
            date.today().strftime("%Y%m%d"),
            _safe_path_segment(task_id, "task_id"),
            "reports",
            "final-report.md",
        )
        physical_path = settings.executor_shared_input_root / relative_path
        physical_path.parent.mkdir(parents=True, exist_ok=True)
        physical_path.write_text(content, encoding="utf-8")
        staged_path = str(physical_path)
        source: dict[str, Any] = {
            "type": "PATH",
            "path": relative_path.as_posix(),
            "sha256": hashlib.sha256(physical_path.read_bytes()).hexdigest(),
        }
    else:
        source = {"type": "INLINE", "content": content}

    payload = ExecutorArtifactRequestBody(
        idempotency_key=f"artifact-report-{execution_id}",
        type="REPORT",
        source=source,
        name="final-report.md",
        description="분석 작업의 최종 결과 리포트",
        media_type="text/markdown",
        append_to_notebook=settings.executor_report_append_to_notebook,
        metadata={"task_id": task_id, "workflow_id": workflow_id},
        actor={"type": "AGENT", "id": task_id},
    ).model_dump(mode="json")
    return payload, staged_path


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload", event)
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return {"message": payload}
        return parsed if isinstance(parsed, dict) else {"message": payload}
    return payload if isinstance(payload, dict) else {}


def _build_step_results(
    *,
    task_id: str,
    execution_id: str | None,
    execution_steps: list[dict[str, Any]],
    execution_events: list[dict[str, Any]],
) -> list[ExecutionStepResult]:
    events_by_step_id: dict[str, dict[str, Any]] = {}
    events_by_execution_id: dict[str, dict[str, Any]] = {}
    for event in execution_events:
        payload = _event_payload(event)
        event_execution_id = payload.get("execution_id")
        event_step_id = payload.get("step_id")
        if isinstance(event_execution_id, str) and event_execution_id:
            events_by_execution_id[event_execution_id] = {
                **payload,
                "event_id": event.get("event_id") or payload.get("event_id"),
            }
        if isinstance(event_step_id, str) and event_step_id:
            events_by_step_id[event_step_id] = {
                **payload,
                "event_id": event.get("event_id") or payload.get("event_id"),
            }

    results: list[ExecutionStepResult] = []
    matched_event_ids: set[str] = set()
    for step in execution_steps:
        lineage = step.get("lineage") or {}
        payload = step.get("payload") or {}
        lineage_inputs = lineage.get("input_parameters") or {}
        lineage_step_id = lineage_inputs.get("step_id") or lineage.get("step_id")
        event = (
            events_by_step_id.get(lineage_step_id)
            if isinstance(lineage_step_id, str)
            else None
        ) or (
            events_by_execution_id.get(execution_id)
            if execution_id
            else None
        ) or {}
        if event:
            event_id = event.get("event_id")
            if isinstance(event_id, str):
                matched_event_ids.add(event_id)
        results.append(
            ExecutionStepResult(
                execution_id=str(
                    event.get("execution_id")
                    or execution_id
                    or f"{task_id}:{step.get('sequence')}"
                ),
                task_id=event.get("task_id", task_id),
                sequence=step.get("sequence"),
                skill=lineage.get("skill_name") or lineage.get("skill"),
                step_id=lineage_step_id,
                tool=lineage.get("tool_name") or lineage.get("tool"),
                tool_id=lineage_inputs.get("tool_id") or lineage.get("tool_id"),
                code_path=(payload.get("source") or {}).get("path")
                or payload.get("path"),
                status=str(event.get("status") or "PENDING"),
                message=event.get("message"),
                event_id=event.get("event_id"),
                raw_payload=_compact_report_payload(event),
            )
        )

    for execution_id, event in events_by_execution_id.items():
        if event.get("event_id") in matched_event_ids:
            continue
        results.append(
            ExecutionStepResult(
                execution_id=execution_id,
                task_id=event.get("task_id", task_id),
                status=str(event.get("status") or "UNKNOWN"),
                message=event.get("message"),
                event_id=event.get("event_id"),
                raw_payload=_compact_report_payload(event),
            )
        )
    return results


def _build_step_results_from_history(
    *,
    task_id: str,
    execution_id: str | None,
    execution_steps: list[dict[str, Any]],
    result_history: list[dict[str, Any]],
) -> list[ExecutionStepResult]:
    """Join authoritative Executor results to submitted cells by sequence."""

    steps_by_sequence = {
        int(step["sequence"]): step
        for step in execution_steps
        if isinstance(step, dict) and step.get("sequence") is not None
    }
    results: list[ExecutionStepResult] = []
    for item in result_history:
        if not isinstance(item, dict):
            continue
        result = item.get("result") or {}
        if not isinstance(result, dict):
            result = {}
        if str(result.get("status") or "") in {
            "NOT_EXECUTED",
            "PENDING",
            "SKIPPED",
        }:
            continue
        sequence_value = result.get("cell_index")
        sequence = int(sequence_value) if sequence_value is not None else None
        step = steps_by_sequence.get(sequence, {}) if sequence is not None else {}
        lineage = step.get("lineage") or {}
        lineage_inputs = lineage.get("input_parameters") or {}
        payload = step.get("payload") or {}
        source = payload.get("source") or {}
        results.append(
            ExecutionStepResult(
                execution_id=str(execution_id or f"{task_id}:{sequence}"),
                task_id=task_id,
                sequence=sequence,
                skill=lineage.get("skill_name") or lineage.get("skill"),
                step_id=result.get("step_id") or lineage_inputs.get("step_id"),
                tool=lineage.get("tool_name") or lineage.get("tool"),
                tool_id=item.get("tool_id") or lineage_inputs.get("tool_id"),
                code_path=(
                    source.get("path")
                    if isinstance(source, dict)
                    else payload.get("path")
                ),
                status=str(result.get("status") or "UNKNOWN"),
                message=result.get("error_message"),
                raw_payload=_compact_report_payload(result),
            )
        )
    return results


def execution_is_reportable(state: AnalysisWorkflowState) -> bool:
    """Only complete, error-free Executor runs produce an analysis report."""

    if state.get("result_collection_error"):
        return False
    if str(state.get("execution_status") or "") != "SUCCEEDED":
        return False
    history = state.get("executor_result_history") or []
    if not history:
        return False
    statuses = [
        str((item.get("result") or {}).get("status") or "")
        for item in history
        if isinstance(item, dict)
    ]
    if "FAILED" in statuses:
        return False
    latest_by_tool: dict[str, str] = {}
    for item in history:
        if not isinstance(item, dict):
            continue
        tool_id = str(item.get("tool_id") or "")
        if tool_id:
            latest_by_tool[tool_id] = str(
                (item.get("result") or {}).get("status") or ""
            )
    return bool(latest_by_tool) and all(
        status == "SUCCEEDED" for status in latest_by_tool.values()
    )


def skip_failed_execution_report(state: AnalysisWorkflowState) -> dict:
    failed = [
        {
            "tool_id": item.get("tool_id"),
            "status": (item.get("result") or {}).get("status"),
            "error_message": (item.get("result") or {}).get("error_message"),
        }
        for item in state.get("executor_result_history") or []
        if isinstance(item, dict)
        and (item.get("result") or {}).get("status") != "SUCCEEDED"
    ]
    message = "실패하거나 완료되지 않은 실행 셀이 있어 분석 리포트 생성을 생략했습니다."
    return {
        "report_status": "skipped_execution_failed",
        "final_response": {
            **state.get("final_response", {}),
            "report_status": "skipped_execution_failed",
            "report_message": message,
            "failed_results": failed,
        },
        "messages": [
            {"role": "assistant", "name": "report_writer", "content": message}
        ],
    }


def make_generate_report(
    deps: AgentDependencies,
    settings: AgentSettings,
    submit_artifact=submit_execution_artifact,
):
    async def generate_report(state: AnalysisWorkflowState) -> dict:
        if deps.report_agent is None:
            raise RuntimeError("report_agent dependency is required")

        task_id = state.get("task_id")
        if not task_id:
            raise RuntimeError("task_id is required to generate a report")
        execution_id = state.get("execution_id")
        if not execution_id:
            raise RuntimeError("execution_id is required to generate a report")

        result_history = state.get("executor_result_history") or []
        step_results = (
            _build_step_results_from_history(
                task_id=task_id,
                execution_id=execution_id,
                execution_steps=state.get("execution_steps") or [],
                result_history=result_history,
            )
            if result_history
            else _build_step_results(
                task_id=task_id,
                execution_id=execution_id,
                execution_steps=state.get("execution_steps") or [],
                execution_events=state.get("execution_events") or [],
            )
        )
        request = ReportGenerationRequest(
            user_request=state.get("user_request") or "",
            analysis_intent=state.get("analysis_intent") or {},
            workflow=effective_workflow_snapshot(
                state.get("workflow") or {},
                state.get("adaptive_runtime_decisions") or {},
            ),
            task_id=task_id,
            execution_id=execution_id,
            step_results=step_results,
        )
        raw_report = await deps.report_agent.ainvoke(context_payload(deps, state, "report_generation", request.model_dump(mode="json")))
        return await finish_report(state, raw_report, task_id, execution_id, step_results)

    async def finish_report(state, raw_report, task_id, execution_id, step_results):
        if isinstance(raw_report, dict):
            content = raw_report.get("content") or raw_report.get("answer")
        else:
            content = raw_report
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("report_agent must return non-empty markdown text")
        report = {"content": content.strip()}

        workflow_id = str(
            (state.get("workflow") or {}).get("workflow", {}).get("id") or "workflow"
        )
        artifact_request, staged_report_path = await asyncio.to_thread(build_report_artifact_request,
            settings,
            execution_id=str(execution_id),
            task_id=task_id,
            workflow_id=workflow_id,
            content=report["content"],
        )
        if settings.executor_submit_enabled:
            artifact_response = await call_async(submit_artifact,
                settings, str(execution_id), artifact_request
            )
        else:
            artifact_response = {
                "status_code": None,
                "body": {},
                "skipped": True,
                "reason": "EXECUTOR_SUBMIT_ENABLED=false",
            }

        artifact_files = dict(state.get("artifact_files", {}))
        report_path = ""
        if settings.demo_artifacts_enabled:
            run_dir = await asyncio.to_thread(build_run_artifact_dir,
                settings,
                user_id=state["user_id"],
                project_id=state["project_id"],
                session_id=state["session_id"],
                task_id=task_id,
            )
            path = await asyncio.to_thread(write_demo_json, run_dir / "analysis_report.json", report)
            report_path = str(path)
            artifact_files["analysis_report"] = report_path
        if staged_report_path:
            artifact_files["executor_report_source"] = staged_report_path

        return {
            "step_results": [
                result.model_dump(mode="json") for result in step_results
            ],
            "analysis_report": report,
            "report_status": "generated",
            "report_artifact_request": artifact_request,
            "report_artifact_response": artifact_response,
            "artifact_files": artifact_files,
            "final_response": {
                "status": "report_generated",
                "task_id": task_id,
                "execution_id": execution_id,
                "report": report,
                "report_path": report_path,
                "report_artifact_request": artifact_request,
                "report_artifact_response": artifact_response,
                "artifact_files": artifact_files,
            },
            "messages": [
                {
                    "role": "assistant",
                    "name": "report_writer",
                    "id": f"report-{task_id}",
                    "content": report["content"],
                }
            ],
        }

    return generate_report


__all__ = [
    "_build_step_results",
    "_build_step_results_from_history",
    "execution_is_reportable",
    "build_report_artifact_request",
    "make_generate_report",
    "skip_failed_execution_report",
]
