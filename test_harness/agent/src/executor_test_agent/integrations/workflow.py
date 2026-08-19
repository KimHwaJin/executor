"""Agent-side commands and reconciliation for asynchronous Executor execution."""

import hashlib
import json
from typing import Any

from mcp import Client

from executor_test_agent.code_policy import PlannedStep
from executor_test_agent.config import AgentSettings
from executor_test_agent.integrations.contracts import (
    AgentExecutionRequest,
    ExecutionEventBatch,
)
from executor_test_agent.integrations.events import event_stream_watermark
from executor_test_agent.integrations.executor import collect_execution_result, required_tool_result


async def submit_execution(
    request: AgentExecutionRequest, settings: AgentSettings
) -> dict[str, Any]:
    """Submit and return immediately; the graph checkpoints before any long wait."""

    watermark = await event_stream_watermark(
        settings.executor_redis_url, settings.executor_event_stream
    )
    async with Client(settings.executor_mcp_url) as client:
        result = await required_tool_result(
            client,
            "execution_submit",
            {"request": request.executor_payload(f"agent-submit-{request.task_id}")},
        )
    return {**result, "event_stream_start_id": watermark}


async def create_execution_operation(
    execution_id: str,
    steps: list[PlannedStep],
    settings: AgentSettings,
    *,
    operation_index: int,
    expected_version: int,
    first_sequence: int,
    actor_id: str = "executor-test-agent",
) -> dict[str, Any]:
    """Append one deterministic follow-up Operation to a waiting MULTI Execution."""

    watermark = await event_stream_watermark(
        settings.executor_redis_url, settings.executor_event_stream
    )
    async with Client(settings.executor_mcp_url) as client:
        source_steps = [
            {
                "sequence": first_sequence + offset,
                "payload": {"type": "CODE", "content": step.code},
                "lineage": {
                    "skill_name": step.skill_name,
                    "tool_name": step.tool_name,
                    "input_parameters": {},
                },
            }
            for offset, step in enumerate(steps)
        ]
        result = await required_tool_result(
            client,
            "execution_operation_create",
            {
                "request": {
                    "execution_id": execution_id,
                    "idempotency_key": _scenario_idempotency_key(
                        execution_id, "operation", operation_index, source_steps
                    ),
                    "expected_version": expected_version,
                    "source": {
                        "type": "INLINE",
                        "spec": {"schema_version": "1.0", "steps": source_steps},
                    },
                    "actor": _agent_actor(actor_id),
                }
            },
        )
    return {**result, "event_stream_start_id": watermark}


async def finalize_execution(
    execution_id: str,
    settings: AgentSettings,
    *,
    expected_version: int,
    actor_id: str = "executor-test-agent",
) -> dict[str, Any]:
    """Finalize a deterministic MULTI scenario after its last Operation."""

    watermark = await event_stream_watermark(
        settings.executor_redis_url, settings.executor_event_stream
    )
    async with Client(settings.executor_mcp_url) as client:
        result = await required_tool_result(
            client,
            "execution_finalize",
            {
                "request": {
                    "execution_id": execution_id,
                    "idempotency_key": _scenario_idempotency_key(execution_id, "finalize", 0, {}),
                    "expected_version": expected_version,
                    "actor": _agent_actor(actor_id),
                }
            },
        )
    return {**result, "event_stream_start_id": watermark}


async def reconcile_execution(
    execution_id: str,
    event_batch: ExecutionEventBatch,
    settings: AgentSettings,
) -> dict:
    """Treat the event as a wake-up and reconcile authoritative results through MCP."""

    wake_event = event_batch.wake_event
    if str(wake_event.aggregate_id) != execution_id:
        raise RuntimeError("Wake-up event does not belong to the interrupted Execution.")
    async with Client(settings.executor_mcp_url) as client:
        result = await collect_execution_result(client, execution_id)

    status = result["execution"]["state"]["status"]
    event_status = wake_event.payload.get("status")
    if status != event_status:
        raise RuntimeError(
            f"Redis wake-up status {event_status!r} does not match Executor state {status!r}."
        )
    return {
        "execution_id": execution_id,
        "wake_event_id": str(wake_event.event_id),
        "wake_event_type": wake_event.event_type,
        "status": status,
        "version": result["execution"]["state"]["version"],
        "runtime_target_id": result["execution"]["runtime"]["target_id"],
        "notebook_path": result["execution"]["workspace"]["notebook_path"],
        "steps": result["steps"],
        "artifacts": result["artifacts"],
        "notebook": result["notebook"],
        "step_events": [
            event.model_dump(mode="json")
            for event in event_batch.events
            if event.event_type in {"execution.step_succeeded", "execution.step_failed"}
        ],
        "operation_events": [
            event.model_dump(mode="json")
            for event in event_batch.events
            if event.event_type in {"execution.operation_succeeded", "execution.operation_failed"}
        ],
    }


def _scenario_idempotency_key(
    execution_id: str,
    action: str,
    index: int,
    payload: object,
) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"agent-scenario-{action}-{execution_id}-{index}-{digest}"


def _agent_actor(actor_id: str = "executor-test-agent") -> dict[str, str]:
    return {"type": "AGENT", "id": actor_id}
