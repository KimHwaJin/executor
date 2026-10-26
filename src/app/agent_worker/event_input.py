"""What a claim Route's LangGraph agent receives, and Executor REST reads."""

from __future__ import annotations

from typing import Any, TypedDict
from uuid import UUID

import httpx

from app.worker import EventContext, ExecutorEvent


class ExecutorApi:
    """Executor REST reads an agent may need beyond the event itself."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        # Base URL is EW_EXECUTOR_BASE_URL (".../api/v1").
        self.http = http

    async def execution(self, execution_id: UUID | str) -> dict[str, Any]:
        """GET /executions/{id}: status, trigger, context, metadata."""
        return await self._get(f"executions/{execution_id}")

    async def result(self, execution_id: UUID | str) -> dict[str, Any]:
        """GET /executions/{id}/result: Operations, Steps and result refs."""
        return await self._get(f"executions/{execution_id}/result")

    async def _get(self, path: str) -> dict[str, Any]:
        response = await self.http.get(path)
        response.raise_for_status()
        return response.json()


class AgentEventInput(TypedDict):
    """Graph input of a claim Route; extend it as the agent's state."""

    tenant: str
    execution_id: str
    event_id: str
    event_type: str
    event_sequence: int
    occurred_at: str
    # payload.status: SUCCEEDED, FAILED or CANCELLED for execution.completed.
    status: str | None
    payload: dict[str, Any]
    # {"trigger": {"type", "actor"}, "context": {"user_id", "task_id", ...}}
    execution: dict[str, Any]
    # The Worker command this run handles; keeps redelivery idempotent.
    ew_command_id: str


def agent_event_input(context: EventContext) -> AgentEventInput:
    event = context.event
    return {
        "tenant": context.tenant,
        "execution_id": str(event.execution_id),
        "event_id": str(event.event_id),
        "event_type": event.event_type,
        "event_sequence": event.event_sequence,
        "occurred_at": event.occurred_at,
        "status": event.payload.get("status"),
        "payload": event.payload,
        # The claim filter only takes events that carry it.
        "execution": event.payload["execution"],
        "ew_command_id": str(context.command_id),
    }


def batch_completion(event: ExecutorEvent) -> bool:
    """Claim filter of the report Route: the end of a BATCH Execution.

    Executor puts ``execution.trigger`` on every execution.completed; events
    before it carry no trigger and are never routed to the claim Route.
    """
    execution = event.payload.get("execution") or {}
    trigger = execution.get("trigger") or {}
    return (
        event.event_type == "execution.completed"
        and trigger.get("type") == "BATCH"
    )


def succeeded(data: AgentEventInput) -> bool:
    """The report agent runs only for Executions that SUCCEEDED."""
    return data["status"] == "SUCCEEDED"
