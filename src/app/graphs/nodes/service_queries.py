"""FAQ answering node."""

from __future__ import annotations

from app.context.builder import context_payload
from app.agents.orchestration.agent_interfaces import ainvoke_typed
from app.agents.orchestration.dependencies import AgentDependencies
from app.graphs.message_utils import as_message_content
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.schemas.agents.orchestration_schema import FaqOutput


def _service_query(state: AnalysisWorkflowState) -> str:
    return (state.get("action_query") or state["user_request"]).strip()


def make_faq_node(deps: AgentDependencies):
    async def faq_node(state: AnalysisWorkflowState) -> dict:
        output = await ainvoke_typed(
            deps.faq_agent,
            context_payload(deps, state, "faq", {"user_request": _service_query(state)}),
            FaqOutput,
        )
        payload = output.model_dump(mode="json")
        # TODO(CRUD): 에이전트 메시지 POST
        return {
            "service_response": {
                "service": "faq",
                "query": _service_query(state),
                "response": payload,
            },
            "messages": [
                {
                    "role": "assistant",
                    "name": "faq",
                    "content": as_message_content(payload),
                }
            ],
        }

    return faq_node
