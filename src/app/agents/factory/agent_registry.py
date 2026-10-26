from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..intent_classifier.intent_classifier_prompt import ANALYSIS_INTENT_PROMPT
from ..router.routing_agent_prompt import ROUTING_AGENT_PROMPT
from ..workflow_generator.workflow_generator_prompt import WORKFLOW_GENERATOR_PROMPT
from ..workflow_generator.workflow_generator_tools import read_skill_documents
from ...schemas.agents.orchestration_schema import (
    AnalysisIntentOutput,
    RoutingOutput,
)
from ...schemas.agents.workflow_generator_schema import WorkflowGenerationOutput


class AgentType(StrEnum):
    ROUTING_AGENT = "ROUTING_AGENT"
    ANALYSIS_INTENT_CLASSIFIER = "ANALYSIS_INTENT_CLASSIFIER"
    WORKFLOW_GENERATOR = "WORKFLOW_GENERATOR"
    REPORT_WRITER = "REPORT_WRITER"

@dataclass(frozen=True)
class AgentCreateSpec:
    name: str
    system_prompt: str
    response_format: Any
    tools: tuple[Any, ...] = ()


AGENT_REGISTRY: dict[AgentType, AgentCreateSpec] = {
    AgentType.ROUTING_AGENT: AgentCreateSpec(
        name="routing_agent",
        system_prompt=ROUTING_AGENT_PROMPT,
        response_format=RoutingOutput,
    ),
    AgentType.ANALYSIS_INTENT_CLASSIFIER: AgentCreateSpec(
        name="analysis_intent_classifier_agent",
        system_prompt=ANALYSIS_INTENT_PROMPT,
        response_format=AnalysisIntentOutput,
    ),
    AgentType.WORKFLOW_GENERATOR: AgentCreateSpec(
        name="workflow_generator_agent",
        system_prompt=WORKFLOW_GENERATOR_PROMPT,
        response_format=WorkflowGenerationOutput,
        tools=(read_skill_documents,),
    ),
}
