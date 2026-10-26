"""Routes of this host and the claim Route LangGraph adapter."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent_worker.claim_adapter import LangGraphClaimAdapter
from app.agent_worker.event_input import (
    AgentEventInput,
    batch_completion,
    succeeded,
)
from app.agent_worker.worker_hooks import ANALYSIS_EVENTS, build_routes
from app.worker import EventContext, ExecutorEvent, IgnoreEvent

EXECUTION = {
    "trigger": {"type": "BATCH", "actor": {"type": "BATCH", "id": "pr-batch"}},
    "context": {"user_id": "user-1", "task_id": "run-7"},
}


def context(payload: dict[str, Any], command_id=None) -> EventContext:
    execution_id = uuid4()
    return EventContext(
        namespace="test",
        tenant="dtest",
        session_id=f"dtest:{execution_id}",
        task_id=None,
        execution_id=execution_id,
        command_id=command_id or uuid4(),
        event=ExecutorEvent(
            event_id=uuid4(),
            execution_id=execution_id,
            event_type="execution.completed",
            event_sequence=5,
            schema_version="1.0",
            occurred_at="2026-10-23T00:00:00+00:00",
            payload=payload,
        ),
    )


class RoutesTests(unittest.TestCase):
    async def handler(self, _: EventContext) -> None:
        return None

    def test_one_tenant_serves_analysis_and_report(self) -> None:
        routes = build_routes(tenant="dtest", analysis=self.handler, report=self.handler)
        self.assertEqual(
            [(r.tenant, r.event_types, r.claim) for r in routes],
            [
                ("dtest", frozenset(ANALYSIS_EVENTS), None),
                ("dtest", frozenset({"execution.completed"}), batch_completion),
            ],
        )

    def test_report_route_only_when_enabled(self) -> None:
        routes = build_routes(tenant="dtest", analysis=self.handler, report=None)
        self.assertEqual([r.claim for r in routes], [None])

    def test_batch_completion_claims_only_batch_terminal_events(self) -> None:
        batch = context({"status": "FAILED", "execution": EXECUTION}).event
        interactive = context(
            {"status": "SUCCEEDED", "execution": {**EXECUTION, "trigger": {"type": "INTERACTIVE"}}}
        ).event
        started = batch.model_copy(update={"event_type": "execution.started"})
        self.assertTrue(batch_completion(batch))
        self.assertFalse(batch_completion(interactive))
        self.assertFalse(batch_completion(started))


class ClaimAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runs: list[AgentEventInput] = []
        self.fail_once = False

        async def agent(state: AgentEventInput) -> dict[str, Any]:
            self.runs.append(dict(state))
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("pod stopped")
            return {}

        builder = StateGraph(AgentEventInput)
        builder.add_node("agent", agent)
        builder.add_edge(START, "agent")
        builder.add_edge("agent", END)
        self.graph = builder.compile(checkpointer=InMemorySaver())

    def test_runs_once_per_command_with_the_event_input(self) -> None:
        adapter = LangGraphClaimAdapter(self.graph, accept=succeeded)
        event = context({"status": "SUCCEEDED", "execution": EXECUTION})

        asyncio.run(adapter(event))
        asyncio.run(adapter(event))

        (run,) = self.runs
        self.assertEqual(run["tenant"], "dtest")
        self.assertEqual(run["execution"], EXECUTION)
        self.assertEqual(run["ew_command_id"], str(event.command_id))

    def test_interrupted_run_resumes_instead_of_restarting(self) -> None:
        adapter = LangGraphClaimAdapter(self.graph, accept=succeeded)
        event = context({"status": "SUCCEEDED", "execution": EXECUTION})
        self.fail_once = True

        with self.assertRaises(RuntimeError):
            asyncio.run(adapter(event))
        asyncio.run(adapter(event))
        asyncio.run(adapter(event))

        self.assertEqual(len(self.runs), 2)
        snapshot = asyncio.run(self.graph.aget_state(event.graph_config))
        self.assertEqual(snapshot.next, ())

    def test_filter_ignores_unsuccessful_executions(self) -> None:
        adapter = LangGraphClaimAdapter(self.graph, accept=succeeded)
        with self.assertRaises(IgnoreEvent):
            asyncio.run(adapter(context({"status": "FAILED", "execution": EXECUTION})))
        self.assertEqual(self.runs, [])


if __name__ == "__main__":
    unittest.main()
