"""Manual adaptive decision and subsequent notebook segment nodes."""

from __future__ import annotations

from app.services.async_io_resources import call_async

import asyncio
import json

from app.context.builder import context_payload
from agent_config import AgentSettings
from app.agents.orchestration.agent_interfaces import ainvoke_typed
from app.agents.orchestration.dependencies import AgentDependencies
from app.graphs.message_utils import as_message_content
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.schemas.agents.orchestration_schema import (
    ConditionalDecisionOutput,
    NotebookGenerationOutput,
)
from app.services.adaptive_workflow import decision_payload
from app.services.demo_artifact_store import build_run_artifact_dir
from app.services.rule_based_notebook_generator import (
    APP_SOURCE_ROOT,
    build_notebook,
    write_cell_code_files,
)
from app.services.workflow_persistence import (
    NullWorkflowStore,
    WorkflowStore,
    effective_workflow_snapshot,
)


def _short_result_summary(result: dict) -> str:
    summary = result.get("output_summary")
    if not summary:
        summary = result.get("error_message") or result.get("outputs") or "요약 없음"
    if isinstance(summary, str):
        text = summary
    else:
        text = json.dumps(summary, ensure_ascii=False, default=str)
    return text if len(text) <= 500 else text[:497] + "..."


def _decision_chat_message(
    decision_summaries: list[dict], next_tool_ids: list[str]
) -> str:
    sections = ["조건부 Tool 판단 결과"]
    for item in decision_summaries:
        sections.extend(
            [
                "",
                f"- 조건 Tool: {item['condition_tool_id']} ({item['status']})",
                f"- 조건 결과 요약: {item['result_summary']}",
                f"- 판단 대상: {item['tool_id']}",
                f"- 결정: {item['decision']}",
                f"- 이유: {item['reason']}",
            ]
        )
    sections.extend(
        [
            "",
            "- 다음 실행 Tool: "
            + (", ".join(next_tool_ids) if next_tool_ids else "없음"),
        ]
    )
    return "\n".join(sections)


def make_decide_conditional_tools(
    deps: AgentDependencies,
    workflow_store: WorkflowStore | None = None,
):
    store = workflow_store or NullWorkflowStore()

    async def decide_conditional_tools(state: AnalysisWorkflowState) -> dict:
        agent = deps.conditional_decision_agent
        if agent is None:
            raise RuntimeError("conditional_decision_agent is not configured")
        pending_tool_id = state.get("adaptive_pending_tool_id")
        if not pending_tool_id:
            raise ValueError("adaptive execution has no pending conditional Tool")
        runtime_decisions = dict(state.get("adaptive_runtime_decisions", {}))
        observations = state.get("adaptive_observations", {})
        new_decisions: list[dict] = []
        decision_summaries: list[dict] = []
        while pending_tool_id:
            try:
                payload = decision_payload(
                    state["workflow"],
                    pending_tool_id,
                    observations,
                )
            except ValueError as exc:
                if "condition Tool result is missing" in str(exc):
                    break
                raise
            output = await ainvoke_typed(agent, context_payload(deps, state, "conditional_decision", payload), ConditionalDecisionOutput)
            decisions = output.model_dump(mode="json")["decisions"]
            if [item["tool_id"] for item in decisions] != [pending_tool_id]:
                raise ValueError(
                    "conditional decision must contain exactly the pending Tool: "
                    f"{pending_tool_id}"
                )
            decision = decisions[0]
            runtime_decisions[pending_tool_id] = decision["decision"]
            new_decisions.append(decision)
            candidate = payload["candidates"][0]
            condition_result = candidate["condition_tool_result"]
            decision_summaries.append(
                {
                    "condition_tool_id": candidate["condition_tool_id"],
                    "status": condition_result.get("status", "UNKNOWN"),
                    "result_summary": _short_result_summary(condition_result),
                    "tool_id": decision["tool_id"],
                    "decision": decision["decision"],
                    "reason": decision.get("reason") or "이유 없음",
                }
            )
            preview = await asyncio.to_thread(build_notebook,
                state["workflow"],
                project_root=APP_SOURCE_ROOT,
                job_id=state.get("task_id", ""),
                executed_tool_ids=set(
                    state.get("adaptive_executed_tool_ids", [])
                ),
                runtime_decisions=runtime_decisions,
                # An excluded conditional Tool may be followed immediately by
                # another unresolved conditional Tool.  No code is expected
                # between those decisions, but the preview still has to expose
                # the next pending Tool.
                allow_empty_adaptive_segment=True,
            )
            pending_tool_id = preview["workflow"].get(
                "pending_conditional_tool_id"
            )
        if not new_decisions:
            raise ValueError("no pending conditional Tool has an available observation")
        effective_workflow = effective_workflow_snapshot(
            state["workflow"], runtime_decisions
        )
        changes = [
            {
                "tool_id": decision["tool_id"],
                "from": "conditional",
                "to": decision["decision"],
                "reason": decision.get("reason"),
            }
            for decision in new_decisions
        ]
        await call_async(store.record_adaptive_round,
            execution_id=str(state.get("execution_id") or ""),
            decision_round=max(1, int(state.get("adaptive_round", 1))),
            changes=changes,
            effective_workflow=effective_workflow,
        )
        next_tool_ids = [
            str(cell["tool_id"])
            for cell in preview.get("cells", [])
            if cell.get("tool_id") and cell.get("role") != "workflow_outputs"
        ]
        history = list(state.get("adaptive_decision_history", [])) + new_decisions
        return {
            "adaptive_runtime_decisions": runtime_decisions,
            "adaptive_execution_plan": {
                "workflow_id": state["workflow"]["workflow"]["id"],
                "runtime_decisions": runtime_decisions,
                "next_tool_ids": next_tool_ids,
            },
            "adaptive_decision_history": history,
            "messages": [
                {
                    "role": "agent",
                    "name": "conditional_decision_agent",
                    "content": _decision_chat_message(
                        decision_summaries, next_tool_ids
                    ),
                }
            ],
        }

    return decide_conditional_tools


def make_build_next_adaptive_code(settings: AgentSettings):
    def build_next_adaptive_code(state: AnalysisWorkflowState) -> dict:
        artifact = build_notebook(
            state["workflow"],
            project_root=APP_SOURCE_ROOT,
            job_id=state["task_id"],
            executed_tool_ids=set(state.get("adaptive_executed_tool_ids", [])),
            runtime_decisions=state.get("adaptive_runtime_decisions", {}),
        )
        output = NotebookGenerationOutput(
            message="다음 분석 단계의 실행 코드를 준비했습니다.",
            cells=artifact["cells"],
        )
        payload = output.model_dump(mode="json")
        artifact_files = dict(state.get("artifact_files", {}))
        round_number = int(state.get("adaptive_round", 1)) + 1
        if settings.demo_artifacts_enabled:
            run_dir = build_run_artifact_dir(
                settings,
                user_id=state["user_id"],
                project_id=state["project_id"],
                session_id=state["session_id"],
                task_id=state["task_id"],
            )
            paths = write_cell_code_files(
                artifact,
                run_dir / f"adaptive_cells_round_{round_number}",
            )
            batches = list(artifact_files.get("adaptive_cell_batches", []))
            batches.append([str(path) for path in paths])
            artifact_files["adaptive_cell_batches"] = batches
        pending_tool_id = artifact["workflow"].get(
            "pending_conditional_tool_id"
        )
        generated_tool_ids = [
            cell["tool_id"] for cell in artifact["cells"] if cell.get("tool_id")
        ]
        return {
            "notebook": payload,
            "artifact_files": artifact_files,
            "adaptive_pending_tool_id": pending_tool_id,
            "adaptive_generated_tool_ids": generated_tool_ids,
            "adaptive_round": round_number,
            "adaptive_status": (
                "waiting_for_execution_results"
                if pending_tool_id
                else "complete"
            ),
            "final_response": {
                "status": (
                    "adaptive_segment_ready"
                    if pending_tool_id
                    else "adaptive_execution_plan_complete"
                ),
                "round": round_number,
                "pending_conditional_tool_id": pending_tool_id,
                "cell_count": len(payload["cells"]),
                "artifact_files": artifact_files,
            },
            "messages": [
                {
                    "role": "agent",
                    "name": "rule_based_notebook_generator",
                    "content": as_message_content(
                        {
                            "message": output.message,
                            "round": round_number,
                            "cell_count": len(output.cells),
                            "pending_conditional_tool_id": pending_tool_id,
                        }
                    ),
                }
            ],
        }

    return build_next_adaptive_code


__all__ = ["make_build_next_adaptive_code", "make_decide_conditional_tools"]
