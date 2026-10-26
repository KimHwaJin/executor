"""Convert Executor notebook cells into Workflow Tool result observations."""

from __future__ import annotations

from typing import Any, Callable

import asyncio
from app.services.async_io_resources import call_async

from agent_config import AgentSettings
from app.services.executor_client import (
    get_execution_notebook,
    get_execution_result,
)


def _current_sequences(state: dict[str, Any]) -> list[int]:
    receipts = state.get("executor_operation_steps") or []
    sequences = [
        int(item["sequence"])
        for item in receipts
        if isinstance(item, dict) and item.get("sequence") is not None
    ]
    if sequences:
        return sorted(set(sequences))

    cell_count = len((state.get("notebook") or {}).get("cells") or [])
    next_sequence = int(state.get("executor_next_sequence", 0))
    return list(range(max(0, next_sequence - cell_count), next_sequence))


async def read_current_operation_tool_results(
    settings: AgentSettings,
    state: dict[str, Any],
    *,
    fetch_notebook: Callable[..., dict[str, Any]] = get_execution_notebook,
) -> list[dict[str, Any]]:
    execution_id = str(state.get("execution_id") or "")
    if not execution_id:
        raise RuntimeError("notebook result collection requires execution_id")
    sequences = _current_sequences(state)
    if not sequences:
        return []

    steps_by_sequence = {
        int(step["sequence"]): step
        for step in state.get("execution_steps") or []
        if isinstance(step, dict) and step.get("sequence") is not None
    }
    cells_by_index: dict[int, dict[str, Any]] = {}
    first = min(sequences)
    last = max(sequences)
    start = first
    while start <= last:
        limit = min(200, last - start + 1)
        response = await call_async(fetch_notebook,
            settings,
            execution_id,
            view="FULL",
            start_index=start,
            limit=limit,
        )
        body = response.get("body") or {}
        for cell in body.get("cells") or []:
            if isinstance(cell, dict) and cell.get("index") is not None:
                cells_by_index[int(cell["index"])] = cell
        start += limit

    missing = [sequence for sequence in sequences if sequence not in cells_by_index]
    if missing:
        raise RuntimeError(f"Executor notebook is missing cells: {missing}")

    results: list[dict[str, Any]] = []
    for sequence in sequences:
        step = steps_by_sequence.get(sequence) or {}
        lineage = step.get("lineage") or {}
        inputs = lineage.get("input_parameters") or {}
        tool_id = inputs.get("tool_id")
        skill_name = lineage.get("skill_name")
        tool_name = lineage.get("tool_name")
        if not tool_id or skill_name == "workflow" or tool_name == "workflow_outputs":
            continue
        cell = cells_by_index[sequence]
        output_summary = cell.get("output_summary") or {}
        if output_summary.get("has_error"):
            status = "FAILED"
        elif cell.get("execution_count") is None:
            status = "NOT_EXECUTED"
        else:
            status = "SUCCEEDED"
        results.append(
            {
                "tool_id": str(tool_id),
                "result": {
                    "status": status,
                    "step_id": inputs.get("step_id"),
                    "cell_index": sequence,
                    "execution_count": cell.get("execution_count"),
                    "output_summary": output_summary,
                    "outputs": cell.get("outputs") or [],
                },
            }
        )
    return results


async def read_current_operation_results_from_api(
    settings: AgentSettings,
    state: dict[str, Any],
    *,
    fetch_result: Callable[..., dict[str, Any]] = get_execution_result,
    fetch_notebook: Callable[..., dict[str, Any]] = get_execution_notebook,
) -> list[dict[str, Any]]:
    """Read authoritative Step status/error and attach available notebook output."""

    execution_id = str(state.get("execution_id") or "")
    if not execution_id:
        raise RuntimeError("execution result collection requires execution_id")
    operation_number = int(state.get("executor_operation_number", 1))
    response = await call_async(fetch_result, settings, execution_id)
    body = response.get("body") or {}
    operation = next(
        (
            item
            for item in body.get("operations") or []
            if isinstance(item, dict)
            and int(item.get("operation_number", 0)) == operation_number
        ),
        None,
    )
    if not isinstance(operation, dict):
        raise RuntimeError(
            f"Executor result is missing operation {operation_number}"
        )
    steps = [
        item
        for item in operation.get("steps") or []
        if isinstance(item, dict)
    ]
    if not steps:
        return []

    sequences = sorted(int(item["sequence"]) for item in steps)
    cells_by_index: dict[int, dict[str, Any]] = {}
    start = min(sequences)
    last = max(sequences)
    while start <= last:
        limit = min(200, last - start + 1)
        notebook_response = await call_async(fetch_notebook,
            settings,
            execution_id,
            view="FULL",
            start_index=start,
            limit=limit,
        )
        for cell in (notebook_response.get("body") or {}).get("cells") or []:
            if isinstance(cell, dict) and cell.get("index") is not None:
                cells_by_index[int(cell["index"])] = cell
        start += limit

    results: list[dict[str, Any]] = []
    for step in sorted(steps, key=lambda item: int(item["sequence"])):
        sequence = int(step["sequence"])
        lineage = step.get("lineage") or {}
        inputs = lineage.get("input_parameters") or {}
        tool_id = str(inputs.get("tool_id") or "")
        if not tool_id:
            continue
        step_result = step.get("result") or {}
        executor_status = str(step_result.get("status") or "PENDING")
        status = (
            "NOT_EXECUTED"
            if executor_status in {"PENDING", "SKIPPED"}
            else executor_status
        )
        cell = cells_by_index.get(sequence) or {}
        results.append(
            {
                "tool_id": tool_id,
                "role": (
                    "workflow_outputs"
                    if lineage.get("skill_name") == "workflow"
                    or lineage.get("tool_name") == "workflow_outputs"
                    else "tool_execution"
                ),
                "result": {
                    "status": status,
                    "executor_status": executor_status,
                    "step_id": inputs.get("step_id"),
                    "executor_step_id": step.get("step_id"),
                    "cell_index": sequence,
                    "execution_count": cell.get("execution_count"),
                    "error_message": step_result.get("error_message"),
                    "output_summary": step_result.get("output_summary") or {},
                    "result_ref": step_result.get("result_ref"),
                    "outputs": cell.get("outputs") or [],
                },
            }
        )
    return results


async def read_current_operation_results(
    settings: AgentSettings,
    state: dict[str, Any],
    *,
    fetch_result: Callable[..., dict[str, Any]] = get_execution_result,
    fetch_notebook: Callable[..., dict[str, Any]] = get_execution_notebook,
) -> list[dict[str, Any]]:
    """Select the configured result source while preserving the API reader."""

    if settings.executor_result_read_mode == "API":
        return await read_current_operation_results_from_api(
            settings,
            state,
            fetch_result=fetch_result,
            fetch_notebook=fetch_notebook,
        )
    if settings.executor_result_read_mode == "MANIFEST":
        from app.services.execution_manifest_reader import (
            read_current_operation_results_from_manifest,
            read_terminal_results_from_manifest,
        )

        event = state.get("execution_event") or {}
        if event.get("event_type") == "execution.completed" or (
            state.get("executor_wait_phase") == "execution_completed"
        ):
            return await read_terminal_results_from_manifest(settings, state, fetch_result=fetch_result)
        return await asyncio.to_thread(read_current_operation_results_from_manifest, settings, state)
    raise ValueError(
        f"unsupported Executor result read mode: "
        f"{settings.executor_result_read_mode!r}"
    )


__all__ = [
    "read_current_operation_results",
    "read_current_operation_results_from_api",
    "read_current_operation_tool_results",
]
