"""Agent recommendation/generation routing without external models or DBs."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent_config import load_agent_settings
from app.agents.orchestration.dependencies import AgentDependencies
from app.graphs.builders.build_analysis_workflow_graph import build_analysis_workflow_graph
from app.graphs.nodes.recommend_workflow import make_recommend_workflow
from app.graphs.routers.orchestration_router import route_workflow_recommendation
from app.services.workflow_recommender import WorkflowRecommender
from app.services.workflow_catalog_service import WorkflowEmbeddingError


DOCUMENT = json.loads((Path(__file__).parent / "fixtures/workflow_standard/static.json").read_text())


class Fixed:
    def __init__(self, value):
        self.value, self.calls = value, []
    def invoke(self, payload):
        self.calls.append(deepcopy(payload))
        return deepcopy(self.value)
    async def ainvoke(self, payload):
        return self.invoke(payload)


class Catalog:
    def __init__(self, score=None, error=None):
        self.searches, self.registrations = [], []
        self.workflow_id, self.version_id = uuid4(), uuid4()
        self.score, self.error = score, error
    async def search(self, query, **kwargs):
        self.searches.append((query, kwargs))
        if self.error:
            raise self.error
        return {"items": [] if self.score is None else [{
            "workflow_id":self.workflow_id, "version_id":self.version_id,
            "revision":4, "similarity":self.score, "workflow":deepcopy(DOCUMENT),
        }]}
    async def register(self, **kwargs):
        self.registrations.append(deepcopy(kwargs))
        return {"workflow_id":uuid4(), "version_id":uuid4(), "revision":1, "recommend_yn":"N"}


def settings(tmp_path, enabled=True):
    return load_agent_settings({"WORKFLOW_RECOMMENDATION_ENABLED":str(enabled).lower(),
                               "WORKFLOW_GENERATION_LIMIT":"1",
                               "WORKFLOW_SIMILARITY_SCORE":"0.8", "DEMO_ARTIFACTS_ENABLED":"false",
                               "DEMO_ARTIFACTS_ROOT":str(tmp_path)})


def state():
    return {"user_id":str(uuid4()), "project_id":str(uuid4()), "session_id":str(uuid4()),
            "task_id":str(uuid4()), "user_request":"NCE 분석 요청", "analysis_context":{},
            "analysis_intent":{"intent":"failure_prediction", "reason":"test"}, "data_selection":{},
            "workflow_revision":0, "workflow_candidates":[]}


def dependencies(catalog):
    generator = Fixed(DOCUMENT)
    deps = AgentDependencies(
        routing_agent=Fixed({"route":"analysis", "reason":"test"}),
        analysis_intent_agent=Fixed({"intent":"failure_prediction", "reason":"test"}),
        workflow_recommender=WorkflowRecommender(catalog_service=catalog, minimum_similarity=0.8),
        workflow_agent=generator, faq_agent=Fixed({}),
        workflow_catalog_service=catalog,
    )
    return deps, generator


@pytest.mark.parametrize("score", [None, 0.79, 0.799999, -0.5])
@pytest.mark.asyncio
async def test_no_matching_or_below_threshold_goes_to_generation(tmp_path, score):
    catalog = Catalog(score)
    deps, generator = dependencies(catalog)
    graph = build_analysis_workflow_graph(deps, settings(tmp_path), checkpointer=InMemorySaver())
    initial = state()
    result = await graph.ainvoke(initial, config={"configurable":{"thread_id":initial["session_id"]}})
    assert result["__interrupt__"]
    assert result["workflow_candidates"][0]["origin"] == "generated"
    assert len(generator.calls) == len(catalog.registrations) == 1
    assert result["recommendation"]["recommendation_available"] is False
    assert catalog.searches[0] == (initial["user_request"], {"limit":3,"threshold":0.8})
    registered = catalog.registrations[0]
    assert registered["registration_type"] == "AGENT" and registered["created_by"] == initial["user_id"]
    assert registered["workflow"]["workflow_version"] == "1.0"
    assert "inputs" not in registered["workflow"]["workflow"]
    assert result["workflow"]["workflow"]["id"] == result["workflow_candidates"][0]["workflow_id"]
    assert result["workflow_version_id"] == result["workflow_candidates"][0]["version_id"]
    assert result["workflow_content_revision"] == 1


@pytest.mark.parametrize("score", [0.8, 0.94])
@pytest.mark.asyncio
async def test_at_or_above_threshold_skips_generator_and_preserves_selected_version(tmp_path, score):
    catalog = Catalog(score)
    deps, generator = dependencies(catalog)
    graph = build_analysis_workflow_graph(deps, settings(tmp_path), checkpointer=InMemorySaver())
    initial = state()
    config = {"configurable":{"thread_id":initial["session_id"]}}
    result = await graph.ainvoke(initial, config=config)
    candidate = result["workflow_candidates"][0]
    assert candidate["origin"] == "recommended"
    assert candidate["revision"] == 4 and candidate["version_id"] == str(catalog.version_id)
    assert candidate["workflow_id"] == str(catalog.workflow_id)
    assert candidate["workflow"] == DOCUMENT
    assert generator.calls == catalog.registrations == []
    result = await graph.ainvoke(Command(resume={"intent":"select","candidate_id":candidate["id"]}), config=config)
    assert result["__interrupt__"]  # Tool input confirmation follows selection.
    assert result["workflow_definition"] == DOCUMENT
    assert result["workflow_version_id"] == str(catalog.version_id)
    assert result["workflow_content_revision"] == 4
    assert result["workflow"]["workflow"]["id"] == str(catalog.workflow_id)


@pytest.mark.asyncio
async def test_generate_action_does_not_repeat_recommendation(tmp_path):
    catalog = Catalog(0.99)
    deps, generator = dependencies(catalog)
    graph = build_analysis_workflow_graph(deps, settings(tmp_path), checkpointer=InMemorySaver())
    initial = state()
    config = {"configurable":{"thread_id":initial["session_id"]}}
    await graph.ainvoke(initial, config=config)
    result = await graph.ainvoke(Command(resume={"intent":"generate"}), config=config)
    assert result["workflow_candidates"][0]["origin"] == "generated"
    assert len(catalog.searches) == len(generator.calls) == 1


@pytest.mark.asyncio
async def test_disabled_recommendation_does_not_call_search(tmp_path):
    catalog = Catalog(0.99)
    deps, generator = dependencies(catalog)
    deps = replace(deps, workflow_catalog_service=None)
    graph = build_analysis_workflow_graph(deps, settings(tmp_path, enabled=False), checkpointer=InMemorySaver())
    initial = state()
    result = await graph.ainvoke(initial, config={"configurable":{"thread_id":initial["session_id"]}})
    assert result["workflow_candidates"][0]["origin"] == "generated"
    assert catalog.searches == [] and len(generator.calls) == 1


@pytest.mark.asyncio
async def test_search_error_does_not_silently_generate(tmp_path):
    catalog = Catalog(error=WorkflowEmbeddingError("provider failed"))
    deps, generator = dependencies(catalog)
    with pytest.raises(WorkflowEmbeddingError):
        await make_recommend_workflow(deps, settings(tmp_path))(state())
    assert generator.calls == catalog.registrations == []


@pytest.mark.asyncio
async def test_sync_injected_recommender_and_score_guard(tmp_path):
    catalog = Catalog()
    deps, _ = dependencies(catalog)
    raw = {"recommendation_available":True, "recommendation":{
        "workflow_id":str(uuid4()), "workflow":deepcopy(DOCUMENT),
        "similarity_score":0.79, "reason":"test"}}
    class SyncOnly:
        def invoke(self, request):
            return raw
    deps = replace(deps, workflow_recommender=SyncOnly())
    result = await make_recommend_workflow(deps, settings(tmp_path))(state())
    assert route_workflow_recommendation(result) == "generate_workflow"


@pytest.mark.parametrize("score", ["nan", "inf", "1.01", "-1.01"])
def test_invalid_similarity_setting(score):
    with pytest.raises(ValueError, match="WORKFLOW_SIMILARITY_SCORE"):
        load_agent_settings({"WORKFLOW_SIMILARITY_SCORE":score})


@pytest.mark.parametrize("limit", [1, 2, 3])
@pytest.mark.asyncio
async def test_multiple_recommendations_and_selected_version(tmp_path, limit):
    class MultiCatalog(Catalog):
        async def search(self, query, **kwargs):
            self.searches.append((query, kwargs))
            return {"items": items}
    items = [{"workflow_id": uuid4(), "version_id": uuid4(), "revision": index + 1,
              "similarity": score, "workflow": deepcopy(DOCUMENT)}
             for index, score in enumerate([0.99, 0.9, 0.8, 0.79])]
    catalog = MultiCatalog()
    deps, generator = dependencies(catalog)
    deps.workflow_recommender.recommendation_limit = limit
    configured = replace(settings(tmp_path), workflow_recommendation_limit=limit)
    graph = build_analysis_workflow_graph(deps, configured, checkpointer=InMemorySaver())
    initial = state()
    config = {"configurable": {"thread_id": initial["session_id"]}}
    result = await graph.ainvoke(initial, config=config)
    candidates = result["workflow_candidates"]
    assert len(candidates) == len(result["recommendation"]["recommendations"]) == limit
    assert catalog.searches[0][1] == {"limit": limit, "threshold": 0.8}
    assert [c["metadata"]["similarity_score"] for c in candidates] == [i["similarity"] for i in items[:limit]]
    assert generator.calls == catalog.registrations == []
    chosen = candidates[-1]
    result = await graph.ainvoke(Command(resume={"intent": "select", "candidate_id": chosen["id"]}), config=config)
    assert result["workflow_version_id"] == str(items[limit - 1]["version_id"])
    assert result["workflow_content_revision"] == limit
    assert result["workflow"]["workflow"]["id"] == str(items[limit - 1]["workflow_id"])


@pytest.mark.parametrize("limit", ["0", "21", "1.5", "abc"])
def test_invalid_recommendation_limit(limit):
    with pytest.raises(ValueError, match="WORKFLOW_RECOMMENDATION_LIMIT"):
        load_agent_settings({"WORKFLOW_RECOMMENDATION_LIMIT": limit})


def test_recommendation_environment_configuration():
    configured = load_agent_settings({"WORKFLOW_SIMILARITY_SCORE": "0.8", "WORKFLOW_RECOMMENDATION_LIMIT": "5"})
    assert configured.workflow_similarity_score == 0.8
    assert configured.workflow_recommendation_limit == 5


@pytest.mark.asyncio
async def test_recommend_again_accumulates_and_keeps_old_candidates_when_exhausted(tmp_path):
    items = [{"workflow_id": uuid4(), "version_id": uuid4(), "revision": index + 1,
              "similarity": score, "workflow": deepcopy(DOCUMENT)}
             for index, score in enumerate([0.99, 0.95, 0.9, 0.85, 0.8, 0.79])]
    class MoreCatalog(Catalog):
        async def search(self, query, **kwargs):
            self.searches.append((query, kwargs))
            excluded = set(kwargs.get("exclude_workflow_ids", []))
            return {"items": [item for item in items if str(item["workflow_id"]) not in excluded
                              and item["similarity"] >= kwargs["threshold"]][:kwargs["limit"]]}
    catalog = MoreCatalog()
    deps, generator = dependencies(catalog)
    graph = build_analysis_workflow_graph(deps, settings(tmp_path), checkpointer=InMemorySaver())
    initial = state()
    config = {"configurable": {"thread_id": initial["session_id"]}}
    first = await graph.ainvoke(initial, config=config)
    old = deepcopy(first["workflow_candidates"])
    assert first["recommendation_search"]["status"] == "matched"
    assert first["recommendation_search"]["added_candidate_ids"] == [c["id"] for c in old]
    assert len(old) == 3
    second = await graph.ainvoke(Command(resume={"intent": "recommend_again"}), config=config)
    candidates = second["workflow_candidates"]
    assert candidates[:3] == old and len(candidates) == 5
    assert second["recommendation_search"]["added_candidate_ids"] == ["recommended_4", "recommended_5"]
    assert len(second["recommendation"]["recommendations"]) == 5
    from app.graphs.nodes.candidate_contract import candidate_display
    public = candidate_display(candidates, can_regenerate=True, can_recommend_again=True).model_dump(mode="json")
    assert len(public["candidates"]) == 5
    assert public["candidates"][4]["similarity_score"] == 0.8
    assert public["candidates"][0]["version_id"] == old[0]["version_id"]
    assert len({item["id"] for item in candidates}) == 5
    assert catalog.searches[1][1]["exclude_workflow_ids"] == [c["workflow_id"] for c in old]
    exhausted = await graph.ainvoke(Command(resume={"intent": "recommend_again"}), config=config)
    assert exhausted["workflow_candidates"] == candidates
    assert exhausted["recommendation"]["recommendation_available"] is True
    search = exhausted["recommendation_search"]
    assert search["status"] == "no_additional_matches" and search["added_candidate_ids"] == []
    assert "추가 후보가 없습니다" in search["message"]
    interrupt = exhausted["__interrupt__"][0].value
    assert interrupt["action_requests"][0]["args"]["recommendation_search"] == search
    from types import SimpleNamespace
    from app.services.workflow_interactions import public_interactions
    projected = public_interactions(SimpleNamespace(run_id=uuid4(), task_id=uuid4(), interrupt=[interrupt]))[0]
    assert projected["payload"]["arguments"]["recommendation_search"] == search
    assert len(projected["payload"]["arguments"]["candidates"]) == 5
    assert "generate" in [a["intent"] for a in projected["payload"]["arguments"]["available_actions"]]
    assert generator.calls == catalog.registrations == []
    # Candidates from earlier rounds remain selectable.
    selected = await graph.ainvoke(Command(resume={"intent": "select", "candidate_id": old[0]["id"]}), config=config)
    assert selected["workflow_version_id"] == old[0]["version_id"]



@pytest.mark.asyncio
async def test_no_additional_matches_generates_only_after_user_action(tmp_path):
    catalog = Catalog(0.8)
    deps, generator = dependencies(catalog)
    graph = build_analysis_workflow_graph(deps, settings(tmp_path), checkpointer=InMemorySaver())
    initial = state()
    config = {"configurable": {"thread_id": initial["session_id"]}}
    first = await graph.ainvoke(initial, config=config)
    exhausted = await graph.ainvoke(Command(resume={"intent": "recommend_again"}), config=config)
    assert exhausted["recommendation_search"]["status"] == "no_additional_matches"
    assert exhausted["workflow_candidates"] == first["workflow_candidates"]
    assert generator.calls == []
    generated = await graph.ainvoke(Command(resume={"intent": "generate"}), config=config)
    assert generated["workflow_candidates"][0]["origin"] == "generated"
    assert len(generator.calls) == 1
    assert "recommendation_search" not in generated["__interrupt__"][0].value["action_requests"][0]["args"]
