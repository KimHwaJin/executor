"""The only place that imports and builds the receiving Agent's graph."""

from __future__ import annotations

from typing import Any

from agent_config import load_agent_settings
from app.agent_worker.event_input import ExecutorApi
from app.agent_worker.graph_boundary import ExecutionBindings
from app.agents.orchestration.dependencies import create_llm_dependencies
from app.graphs.builders.build_analysis_workflow_graph import (
    build_analysis_workflow_graph,
)
from app.graphs.builders.build_report_graph import build_report_graph
from app.services.workflow_persistence import workflow_store_from_environment


def build_agent_graph(
    *,
    bindings: ExecutionBindings,
    checkpointer: Any,
) -> Any:
    """Compile the service graph with Worker bindings and its checkpointer."""

    settings = load_agent_settings()
    dependencies = create_llm_dependencies(settings)
    workflow_store = workflow_store_from_environment()
    return build_analysis_workflow_graph(
        dependencies,
        settings,
        bindings=bindings,
        checkpointer=checkpointer,
        workflow_store=workflow_store,
    )


def build_report_agent_graph(
    *,
    checkpointer: Any,
    executor_api: ExecutorApi,
) -> Any:
    """Compile the report graph run for this tenant's successful batches."""

    return build_report_graph(
        checkpointer=checkpointer,
        executor_api=executor_api,
    )
