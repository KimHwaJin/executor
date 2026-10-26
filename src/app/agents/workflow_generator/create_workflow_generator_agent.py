"""LangChain Workflow Generator agent construction."""

from __future__ import annotations

import json
from typing import Any

from langchain.agents import create_agent
from app.agents.factory.agent_registry import AGENT_REGISTRY, AgentType


def workflow_system_prompt() -> str:
    spec = AGENT_REGISTRY[AgentType.WORKFLOW_GENERATOR]
    schema = spec.response_format.model_json_schema()
    return (
        f"{spec.system_prompt}\n\n"
        "Using the provided workflow_resources, return exactly one "
        "JSON object matching the following JSON Schema. Do not return "
        "markdown or explanatory text.\n"
        f"JSON Schema: {json.dumps(schema, ensure_ascii=False)}"
    )


def create_workflow_generator_agent(
    model: Any,
    *,
    structured_output_mode: str = "prompt_json",
):
    """Create the Workflow Generator from its centralized registry spec."""
    spec = AGENT_REGISTRY[AgentType.WORKFLOW_GENERATOR]
    system_prompt = spec.system_prompt
    agent_kwargs = {
        "model": model,
        "tools": [],
        "system_prompt": system_prompt,
        "name": spec.name,
    }
    schema = spec.response_format.model_json_schema()
    agent_kwargs["system_prompt"] = workflow_system_prompt()
    if structured_output_mode == "provider_json_schema":
        agent_kwargs["model"] = model.bind(
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "strict": True,
                    "name": spec.response_format.__name__,
                    "schema": schema,
                },
            }
        )
    elif structured_output_mode != "prompt_json":
        raise ValueError(
            f"Unsupported workflow structured output mode: {structured_output_mode!r}"
        )
    return create_agent(**agent_kwargs)


__all__ = ["create_workflow_generator_agent"]
