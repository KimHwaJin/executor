from typing import Any

from langchain.agents import create_agent

from .agent_registry import AGENT_REGISTRY, AgentType


class AgentFactory:
    @staticmethod
    def create(agent_type: AgentType, model: Any):
        spec = AGENT_REGISTRY.get(agent_type)

        if spec is None:
            raise ValueError(f"등록되지 않은 agent type입니다: {agent_type}")

        return create_agent(
            model=model,
            tools=list(spec.tools),
            system_prompt=spec.system_prompt,
            name=spec.name,
            response_format=spec.response_format,
        )