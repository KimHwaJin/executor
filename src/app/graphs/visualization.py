"""Print or export the user-agent LangGraph as Mermaid syntax."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Sequence


def graph_to_mermaid(compiled_graph: Any, *, with_styles: bool = True) -> str:
    """Return the compiled graph topology as Mermaid syntax."""
    return compiled_graph.get_graph().draw_mermaid(with_styles=with_styles)


def export_graph_mermaid(
    compiled_graph: Any,
    output_path: str | Path,
    *,
    with_styles: bool = True,
) -> Path:
    """Write Mermaid syntax to a UTF-8 .mmd file and return its path."""
    path = Path(output_path)
    if path.suffix.lower() not in {".mmd", ".mermaid"}:
        raise ValueError("output_path must use .mmd or .mermaid")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        graph_to_mermaid(compiled_graph, with_styles=with_styles),
        encoding="utf-8",
    )
    return path


class _TopologyOnlyAgent:
    """Agent stub used only to compile graph topology for visualization."""

    def invoke(self, payload: Any) -> Any:
        raise RuntimeError("Topology-only agents must not be invoked")


def build_visualization_graph():
    """Compile the graph without model credentials or a database connection."""
    from agent_config import load_agent_settings
    from app.agents.orchestration.dependencies import AgentDependencies
    from app.graphs.builders.build_analysis_workflow_graph import (
        build_analysis_workflow_graph,
    )

    agent = _TopologyOnlyAgent()
    dependencies = AgentDependencies(
        routing_agent=agent,
        analysis_intent_agent=agent,
        workflow_recommender=agent,
        workflow_agent=agent,
        faq_agent=agent,
    )
    return build_analysis_workflow_graph(
        dependencies,
        load_agent_settings(),
        checkpointer=None,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Print or save the user-agent LangGraph Mermaid diagram."
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Optional .mmd or .mermaid output path. Without it, print to stdout.",
    )
    parser.add_argument(
        "--no-styles",
        action="store_true",
        help="Generate Mermaid without LangGraph style definitions.",
    )
    args = parser.parse_args(argv)

    graph = build_visualization_graph()
    with_styles = not args.no_styles
    if args.output:
        print(export_graph_mermaid(graph, args.output, with_styles=with_styles))
        return
    print(graph_to_mermaid(graph, with_styles=with_styles))


if __name__ == "__main__":
    main()


__all__ = [
    "build_visualization_graph",
    "export_graph_mermaid",
    "graph_to_mermaid",
    "main",
]
