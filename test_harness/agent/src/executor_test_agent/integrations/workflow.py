"""Agent-side commands and reconciliation for asynchronous Executor execution."""

from mcp import Client

from executor_test_agent.config import AgentSettings
from executor_test_agent.integrations.contracts import (
    AgentExecutionRequest,
    ExecutionEventEnvelope,
)
from executor_test_agent.integrations.executor import collect_execution_result, required_tool_result


async def submit_execution(request: AgentExecutionRequest, settings: AgentSettings) -> str:
    """Submit and return immediately; the graph checkpoints before any long wait."""
    async with Client(settings.executor_mcp_url) as client:
        submitted = await required_tool_result(
            client,
            "execution_submit",
            {"request": request.executor_payload(f"agent-submit-{request.task_id}")},
        )
    return str(submitted["execution_id"])


async def reconcile_execution(
    execution_id: str,
    terminal_event: ExecutionEventEnvelope,
    settings: AgentSettings,
) -> dict:
    """Treat the event as a wake-up and reconcile authoritative results through MCP."""
    if str(terminal_event.aggregate_id) != execution_id:
        raise RuntimeError("Terminal event does not belong to the interrupted execution.")
    async with Client(settings.executor_mcp_url) as client:
        result = await collect_execution_result(client, execution_id)

    status = result["execution"]["state"]["status"]
    event_status = terminal_event.payload.get("status")
    if status != event_status:
        raise RuntimeError(
            f"Redis terminal status {event_status!r} does not match Executor state {status!r}."
        )
    return {
        "execution_id": execution_id,
        "terminal_event_id": str(terminal_event.event_id),
        "terminal_event_type": terminal_event.event_type,
        "status": status,
        "runtime_target_id": result["execution"]["runtime"]["target_id"],
        "notebook_path": result["execution"]["workspace"]["notebook_path"],
        "steps": result["steps"],
        "artifacts": result["artifacts"],
        "notebook": result["notebook"],
    }
