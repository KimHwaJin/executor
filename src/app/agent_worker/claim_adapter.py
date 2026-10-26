"""Run a LangGraph agent for an event of a claim Route."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.agent_worker.event_input import AgentEventInput, agent_event_input
from app.worker import EventContext, IgnoreEvent, RejectEvent


class LangGraphClaimAdapter:
    """Invoke the graph once per event on thread "{tenant}:{execution_id}".

    The graph state must extend AgentEventInput so ``ew_command_id`` is
    checkpointed: a redelivered command then resumes or skips instead of
    running the agent again.
    """

    def __init__(
        self,
        graph: Any,
        *,
        accept: Callable[[AgentEventInput], bool] | None = None,
    ) -> None:
        self.graph = graph
        self.accept = accept

    async def __call__(self, context: EventContext) -> None:
        config = context.graph_config
        command_id = str(context.command_id)
        snapshot = await self.graph.aget_state(config)
        if snapshot.values.get("ew_command_id") == command_id:
            if snapshot.next:
                # The previous delivery stopped mid-run; continue it.
                await self.graph.ainvoke(None, config, durability="sync")
            return
        if snapshot.next:
            raise RejectEvent("A claim Route graph must not wait for input")
        data = agent_event_input(context)
        if self.accept is not None and not self.accept(data):
            raise IgnoreEvent("Route filter declined the event")
        await self.graph.ainvoke(data, config, durability="sync")
