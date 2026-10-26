"""LangChain Routing Agent construction."""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent

from app.agents.factory.agent_registry import AGENT_REGISTRY, AgentType


def create_routing_agent(model: Any):
    """Create the Routing Agent from its registry specification."""
    spec = AGENT_REGISTRY[AgentType.ROUTING_AGENT]
    return create_agent(
        model=model,
        tools=list(spec.tools),
        system_prompt=spec.system_prompt,
        response_format=spec.response_format,
        name=spec.name,
    )


__all__ = ["create_routing_agent"]
