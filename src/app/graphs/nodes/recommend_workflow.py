"""Recommend a stored Workflow through an injected search service."""

from __future__ import annotations
from copy import deepcopy

from agent_config import AgentSettings
from app.graphs.message_utils import as_message_content
from app.agents.orchestration.dependencies import AgentDependencies
from app.graphs.state.analysis_workflow_state import AnalysisWorkflowState
from app.schemas.agents.workflow_recommender_schema import (
    WorkflowRecommendationOutput,
)
from app.services.async_io_resources import call_async
from app.services.demo_artifact_store import build_workflow_output_dir
from app.agents.workflow_generator.workflow_standard_compiler import compile_workflow


def make_recommend_workflow(
    deps: AgentDependencies,
    settings: AgentSettings,
):
    async def recommend_workflow(state: AnalysisWorkflowState) -> dict:
        previous = (deepcopy(state.get("workflow_candidates", []))
                    if state.get("append_workflow_recommendations") else [])
        previous = [item for item in previous if item["origin"] == "recommended"]
        request = {
            "user_request": state["user_request"],
            "analysis_intent": state.get("analysis_intent", {}),
            "analysis_context": state.get("analysis_context", {}),
            "data_selection": state.get("data_selection", {}),
            "excluded_workflow_candidates": state.get("excluded_workflow_candidates", []),
        }
        if previous:
            request["exclude_workflow_ids"] = [item["workflow_id"] for item in previous]
        raw_recommendation = None
        if settings.workflow_recommendation_enabled and not state.get("skip_workflow_recommendation"):
            callback = getattr(deps.workflow_recommender, "ainvoke", None) or deps.workflow_recommender.invoke
            raw_recommendation = await call_async(callback, request)
        if raw_recommendation:
            output = WorkflowRecommendationOutput.model_validate(
                raw_recommendation
            )
        else:
            output = WorkflowRecommendationOutput(
                recommendation_available=False,
                recommendation=None,
                no_match_reason=(
                    "조건에 맞는 추천 워크플로우 후보를 찾지 못했습니다."
                ),
            )
        eligible = [item for item in output.recommendations
                    if item.similarity_score >= settings.workflow_similarity_score][:settings.workflow_recommendation_limit]
        output = (WorkflowRecommendationOutput(recommendation_available=True, recommendations=eligible)
                  if eligible else WorkflowRecommendationOutput(
                      recommendation_available=False,
                      no_match_reason="추천 유사도 기준을 충족하는 후보가 없어 새 워크플로우를 생성합니다.",
                  ))
        existing_ids = {item["workflow_id"] for item in previous}
        eligible = [item for item in eligible if item.workflow_id not in existing_ids]
        existing = [{**item["metadata"], "workflow": item["workflow"]} for item in previous]
        combined = existing + [item.model_dump(mode="json") for item in eligible]
        output = (WorkflowRecommendationOutput(recommendation_available=True, recommendations=combined)
                  if combined else output)
        payload = output.model_dump(mode="json")
        previous_count = len(previous)
        candidates = previous
        updates = {"skip_workflow_recommendation": False, "append_workflow_recommendations": False}
        if output.recommendation_available:
            # HITL uses a positive proposal revision, distinct from DB content revision.
            updates["workflow_revision"] = max(1, state.get("workflow_revision", 0))
            updates["artifact_output_dir"] = await call_async(build_workflow_output_dir, settings, state)
            for index, item in enumerate([item.model_dump(mode="json") for item in eligible], start=len(previous) + 1):
                recommendation = deepcopy(item)
                workflow = recommendation.pop("workflow")
                compiled = (await call_async(compile_workflow, workflow, workflow_id=recommendation["workflow_id"])
                            if workflow.get("workflow_version") == "1.0" else deepcopy(workflow))
                candidates.append(
                    {
                        "id": f"recommended_{index}",
                        "origin": "recommended",
                        "status": compiled["workflow"]["status"],
                        "workflow": workflow,
                        "workflow_id": recommendation["workflow_id"],
                        "version_id": recommendation["version_id"],
                        "revision": recommendation["revision"],
                        "execution_inputs": [],
                        "metadata": recommendation,
                    }
                )
        search_result = {
            "status": "matched" if eligible else ("no_additional_matches" if previous_count else "no_matches"),
            "added_candidate_ids": [item["id"] for item in candidates[previous_count:]],
            "message": ("기준을 충족하는 추가 후보가 없습니다. 기존 후보를 선택하거나 새 워크플로우를 생성할 수 있습니다."
                        if previous_count and not eligible else None),
        }
        # TODO(CRUD): 에이전트 메시지 POST
        return {
            **updates,
            "recommendation": payload,
            "recommendation_search": search_result,
            "workflow_candidates": candidates,
            "messages": [
                {
                    "role": "assistant",
                    "name": "workflow_recommender",
                    "content": as_message_content(
                        {
                            "status": "candidate_search_complete",
                            "result": payload,
                            "recommendation_search": search_result,
                        }
                    ),
                }
            ],
        }

    return recommend_workflow
