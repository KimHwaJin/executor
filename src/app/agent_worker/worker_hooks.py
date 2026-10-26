"""Executor event Routes of this host: which events reach which agent."""

from __future__ import annotations

from app.agent_worker.event_input import batch_completion
from app.worker import Route
from app.worker.contracts import EventHandler

# The analysis graph waits for these after submitting (executor_wait_phase).
ANALYSIS_EVENTS = {"execution.operation_completed", "execution.completed"}


def build_routes(
    *,
    tenant: str,
    analysis: EventHandler,
    report: EventHandler | None,
) -> list[Route]:
    """Edit this registry to add an agent or the events it handles.

    One tenant serves this service: the analysis graph's own Executions
    (INTERACTIVE, registered at submit) and the batches registered in
    pr-batch with the same tenant_id (BATCH, claimed on completion).
    """

    routes = [Route(tenant, ANALYSIS_EVENTS, analysis)]
    if report is not None:
        routes.append(
            Route(tenant, {"execution.completed"}, report, claim=batch_completion)
        )
    return routes
