"""Report agent run by the Worker when a batch Execution succeeds.

Skeleton only: the report agent developer replaces ``write_report`` (and adds
nodes). The Worker invokes the graph with an AgentEventInput on thread
"{tenant}:{execution_id}"; read the Step results with ``executor_api.result``.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from app.agent_worker.event_input import AgentEventInput, ExecutorApi


class ReportState(AgentEventInput, total=False):
    report: dict[str, Any]


def build_report_graph(*, checkpointer: Any, executor_api: ExecutorApi) -> Any:
    async def write_report(state: ReportState) -> dict[str, Any]:
        raise NotImplementedError(
            "Report agent is not implemented: replace write_report "
            f"(execution {state['execution_id']})"
        )

    builder = StateGraph(ReportState)
    builder.add_node("write_report", write_report)
    builder.add_edge(START, "write_report")
    builder.add_edge("write_report", END)
    return builder.compile(checkpointer=checkpointer)
