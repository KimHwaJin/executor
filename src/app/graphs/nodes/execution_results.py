"""Collect Executor results after a Redis event resumes the graph."""

from __future__ import annotations


from app.services.async_io_resources import call_async

from typing import Any, Callable
import logging

from agent_config import AgentSettings
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.services.execution_notebook_reader import (
    read_current_operation_results,
)
from app.services.workflow_persistence import (
    NullWorkflowStore,
    WorkflowStore,
    effective_workflow_snapshot,
)

logger = logging.getLogger(__name__)


def make_collect_execution_results(
    settings: AgentSettings,
    *,
    adaptive: bool,
    read_results: Callable[
        [AgentSettings, dict[str, Any]], list[dict[str, Any]]
    ] = read_current_operation_results,
    workflow_store: WorkflowStore | None = None,
):
    store = workflow_store or NullWorkflowStore()

    async def collect_execution_results(state: AnalysisWorkflowState) -> dict:
        execution_id = str(state.get("execution_id") or "")
        if not execution_id:
            raise RuntimeError("automatic result collection requires execution_id")
        event = state.get("execution_event")
        if not isinstance(event, dict):
            raise RuntimeError(
                "execution_event is required after the Redis worker resumes "
                "the graph"
            )
        event_status = str(event.get("execution_status") or event["status"])
        event_state_version = int(event["state_version"])
        collection_error = ""
        try:
            tool_results = await call_async(read_results, settings, state)
        except Exception as exc:
            # Result failures must not prevent acknowledging a terminal event.
            logger.exception("Executor result collection failed execution_id=%s", execution_id)
            tool_results = []
            collection_error = f"{type(exc).__name__}: {exc}"
        error = (event.get("response") or {}).get("error") or {}
        errors = list(dict.fromkeys(str(message) for message in [
            error.get("message"),
            *[(item.get("result") or {}).get("error_message") for item in tool_results],
        ] if message))
        message = ""
        if event_status == "FAILED":
            message = "분석 실행에 실패했습니다."
        elif event_status == "CANCELLED":
            message = "분석 실행이 취소되었습니다."
        elif collection_error:
            message = "분석 실행 결과를 읽는 중 오류가 발생했습니다."
        if errors:
            message += " 원인: " + " | ".join(errors)
        if collection_error:
            message += " 결과 수집 오류: " + collection_error
        update: dict = {
            "execution_event": None,
            "executor_state_version": event_state_version,
            "executor_requires_state_version": False,
            "executor_tool_results": tool_results,
            "executor_result_history": [
                *state.get("executor_result_history", []),
                *tool_results,
            ],
            "execution_status": event_status,
            "result_collection_error": collection_error,
            "final_response": {
                "status": (
                    "adaptive_execution_results_collected"
                    if adaptive
                    else "static_execution_results_collected"
                ),
                "execution_id": execution_id,
                "execution_status": event_status,
                "execution_state_version": event_state_version,
                "results": tool_results,
                "errors": errors,
                "result_collection_error": collection_error or None,
                "message": message,
            },
        }
        if message:
            update["messages"] = [{"role": "assistant", "name": "executor_result", "content": message}]
        if adaptive:
            observations = dict(state.get("adaptive_observations", {}))
            for item in tool_results:
                if item.get("role") != "workflow_outputs":
                    observations[item["tool_id"]] = item["result"]
            executed = list(state.get("adaptive_executed_tool_ids", []))
            executed_result_ids = [
                item["tool_id"]
                for item in tool_results
                if item.get("role") != "workflow_outputs"
                and (item.get("result") or {}).get("status")
                not in {"NOT_EXECUTED", "PENDING", "SKIPPED"}
            ]
            for tool_id in executed_result_ids:
                if tool_id not in executed:
                    executed.append(tool_id)
            update.update(
                {
                    "adaptive_observations": observations,
                    "adaptive_executed_tool_ids": executed,
                }
            )
        if not adaptive or event_status in {"FAILED", "CANCELLED"}:
            await call_async(store.finish_execution,
                execution_id=execution_id,
                status=event_status,
                final_workflow=effective_workflow_snapshot(
                    state.get("workflow", {}), state.get("adaptive_runtime_decisions", {})
                ),
                successful=(
                    event_status == "SUCCEEDED"
                    and not collection_error
                    and not any(
                        (item.get("result") or {}).get("status") == "FAILED"
                        for item in tool_results
                    )
                ),
            )
        return update

    return collect_execution_results


__all__ = ["make_collect_execution_results"]
