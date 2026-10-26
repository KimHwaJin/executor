"""LangGraph dev entrypoint for local Studio testing."""

from __future__ import annotations

import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from agent_config import load_agent_settings
from app.agent_worker.api_bridge import get_api_worker_bridge
from app.agents.orchestration.dependencies import create_llm_dependencies
from app.graphs.builders.build_analysis_workflow_graph import (
    build_analysis_workflow_graph,
)
from app.services.workflow_persistence import workflow_store_from_environment


settings = load_agent_settings()
dependencies = create_llm_dependencies(settings)
api_worker_bridge = get_api_worker_bridge()
graph = build_analysis_workflow_graph(
    dependencies,
    settings,
    checkpointer=None,
    bindings=api_worker_bridge.bindings,
    workflow_store=workflow_store_from_environment(),
).with_config(configurable={"agent_chat_input_form": True})


__all__ = ["graph"]
