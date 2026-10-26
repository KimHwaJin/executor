"""User-agent LangGraph package."""

from app.graphs.builders.build_analysis_workflow_graph import (
    build_analysis_workflow_graph,
    build_user_agent_graph,
)
from app.graphs.checkpoint import compiled_in_memory_graph, compiled_postgres_graph


def build_visualization_graph(*args, **kwargs):
    from app.graphs.visualization import build_visualization_graph as implementation

    return implementation(*args, **kwargs)


def export_graph_mermaid(*args, **kwargs):
    from app.graphs.visualization import export_graph_mermaid as implementation

    return implementation(*args, **kwargs)


def graph_to_mermaid(*args, **kwargs):
    from app.graphs.visualization import graph_to_mermaid as implementation

    return implementation(*args, **kwargs)


__all__ = [
    "build_analysis_workflow_graph",
    "build_user_agent_graph",
    "build_visualization_graph",
    "compiled_in_memory_graph",
    "compiled_postgres_graph",
    "export_graph_mermaid",
    "graph_to_mermaid",
]
