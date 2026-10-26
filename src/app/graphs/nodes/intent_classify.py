"""Structured analysis-intent classification node."""

from __future__ import annotations

from app.context.builder import context_payload
from agent_config import ENABLED_ANALYSIS_INTENTS
from app.agents.orchestration.agent_interfaces import ainvoke_typed
from app.agents.orchestration.dependencies import AgentDependencies
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.schemas.agents.orchestration_schema import AnalysisIntentOutput


def make_classify_analysis_intent(deps: AgentDependencies):
    async def classify_analysis_intent(state: AnalysisWorkflowState) -> dict:
        if len(ENABLED_ANALYSIS_INTENTS) == 1:
            output = AnalysisIntentOutput(
                intent=ENABLED_ANALYSIS_INTENTS[0],
                reason="현재 활성화된 단일 분석 의도로 분류했습니다.",
            )
        else:
            output = await ainvoke_typed(
                deps.analysis_intent_agent,
                context_payload(deps, state, "intent", {
                    "user_request": state["user_request"],
                    "enabled_analysis_intents": ENABLED_ANALYSIS_INTENTS,
                }),
                AnalysisIntentOutput,
            )
            if output.intent not in ENABLED_ANALYSIS_INTENTS:
                raise ValueError(
                    f"활성화되지 않은 분석 의도입니다: {output.intent}"
                )
        payload = output.model_dump(mode="json")
        # TODO(CRUD): 에이전트 메시지 POST
        return {
            "analysis_intent": payload,
        }

    return classify_analysis_intent


__all__ = ["make_classify_analysis_intent"]
