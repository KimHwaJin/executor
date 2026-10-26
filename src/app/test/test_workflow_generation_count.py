"""Configurable generation batches through the real graph and candidate HITL."""
from copy import deepcopy
from dataclasses import replace

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent_config import load_agent_settings
from app.graphs.builders.build_analysis_workflow_graph import build_analysis_workflow_graph
from app.graphs.nodes.candidate_contract import candidate_fingerprint
from app.test.test_workflow_recommendation_integration import (
    Catalog, DOCUMENT, Fixed, dependencies, settings, state,
)


class DistinctGenerator(Fixed):
    def invoke(self, payload):
        self.calls.append(deepcopy(payload))
        document = deepcopy(DOCUMENT)
        tools = document["workflow"]["steps"][-1]["tools"]
        tools.extend(deepcopy(tools[-1]) for _ in range(len(self.calls) - 1))
        return document


def build(tmp_path, count):
    catalog = Catalog()
    deps, _ = dependencies(catalog)
    generator = DistinctGenerator(DOCUMENT)
    deps = replace(deps, workflow_agent=generator)
    configured = replace(settings(tmp_path), workflow_generation_limit=count)
    graph = build_analysis_workflow_graph(deps, configured, checkpointer=InMemorySaver())
    initial = state()
    return graph, initial, {"configurable": {"thread_id": initial["session_id"]}}, catalog, generator


@pytest.mark.parametrize("count", [1, 2, 3])
@pytest.mark.asyncio
async def test_generate_requested_count_and_select_any_candidate(tmp_path, count):
    graph, initial, config, catalog, generator = build(tmp_path, count)
    result = await graph.ainvoke(initial, config)
    candidates = result["workflow_candidates"]
    assert len(candidates) == len(generator.calls) == len(catalog.registrations) == count
    assert len({c["id"] for c in candidates}) == count
    assert len({c["workflow_id"] for c in candidates}) == count
    assert len({c["version_id"] for c in candidates}) == count
    assert len({candidate_fingerprint(c["workflow"]) for c in candidates}) == count
    assert all(c["revision"] == 1 for c in candidates)
    assert result["workflow_revision"] == 1  # A generation round, not candidate count.
    assert result["generated_workflow_drafts"] == []
    args = result["__interrupt__"][0].value["action_requests"][0]["args"]
    assert len(args["candidates"]) == count
    assert [a["intent"] for a in args["available_actions"]] == ["select", "regenerate", "reject"]
    selected = candidates[-1]
    resumed = await graph.ainvoke(Command(resume={"intent": "select", "candidate_id": selected["id"]}), config)
    assert resumed["workflow_id"] == selected["workflow_id"]
    assert resumed["workflow_version_id"] == selected["version_id"]
    assert resumed["workflow_definition"] == selected["workflow"]
    assert resumed["__interrupt__"]  # Input confirmation is still required.


@pytest.mark.asyncio
async def test_regenerate_replaces_batch_and_excludes_all_previous_candidates(tmp_path):
    graph, initial, config, catalog, generator = build(tmp_path, 2)
    first = await graph.ainvoke(initial, config)
    old = first["workflow_candidates"]
    second = await graph.ainvoke(Command(resume={"intent": "regenerate"}), config)
    current = second["workflow_candidates"]
    assert len(current) == 2 and len(catalog.registrations) == 4
    assert len(second["excluded_workflow_candidates"]) == 2
    assert second["workflow_revision"] == 2
    assert {c["id"] for c in old}.isdisjoint(c["id"] for c in current)
    assert {candidate_fingerprint(c["workflow"]) for c in old}.isdisjoint(
        candidate_fingerprint(c["workflow"]) for c in current)
    assert len(generator.calls[2]["excluded_workflows"]) == 2
    assert len(generator.calls[3]["excluded_workflows"]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [2, 3])
async def test_duplicate_additional_candidate_preserves_valid_candidate_and_notice(tmp_path, count):
    graph, initial, config, catalog, _ = build(tmp_path, count)
    # Fixed generator repeats the first structure even when exclusions are given.
    deps, generator = dependencies(catalog)
    graph = build_analysis_workflow_graph(
        deps, replace(settings(tmp_path), workflow_generation_limit=count), checkpointer=InMemorySaver())
    result = await graph.ainvoke(initial, config)
    assert len(generator.calls) == 4  # First candidate + three retries for second.
    assert len(catalog.registrations) == len(result["workflow_candidates"]) == 1
    assert f"요청한 {count}개" in result["workflow_generation_notice"]
    assert "1개" in result["workflow_generation_notice"]
    args = result["__interrupt__"][0].value["action_requests"][0]["args"]
    assert args["candidates"][0]["reason"] == result["workflow_generation_notice"]
    selected = result["workflow_candidates"][0]
    resumed = await graph.ainvoke(Command(resume={"intent": "select", "candidate_id": selected["id"]}), config)
    assert resumed["workflow_version_id"] == selected["version_id"]
    assert resumed["__interrupt__"]


@pytest.mark.asyncio
async def test_duplicate_retry_can_recover_and_fill_requested_count(tmp_path):
    graph, initial, config, catalog, _ = build(tmp_path, 2)
    deps, _ = dependencies(catalog)

    class RetryGenerator(DistinctGenerator):
        def invoke(self, payload):
            if len(self.calls) == 1:
                self.calls.append(deepcopy(payload))
                return deepcopy(DOCUMENT)
            return super().invoke(payload)

    generator = RetryGenerator(DOCUMENT)
    graph = build_analysis_workflow_graph(replace(deps, workflow_agent=generator),
        replace(settings(tmp_path), workflow_generation_limit=2), checkpointer=InMemorySaver())
    result = await graph.ainvoke(initial, config)
    assert len(result["workflow_candidates"]) == len(catalog.registrations) == 2
    assert len(generator.calls) == 3
    assert generator.calls[-1]["previous_rejected_workflow"]["workflow"]["steps"] == DOCUMENT["workflow"]["steps"]
    assert result["workflow_generation_notice"] is None
    assert not any("요청한 2개 중" in c["reason"] for c in
        result["__interrupt__"][0].value["action_requests"][0]["args"]["candidates"])


@pytest.mark.asyncio
async def test_invalid_additional_candidate_is_not_treated_as_duplicate(tmp_path):
    graph, initial, config, catalog, _ = build(tmp_path, 2)
    deps, _ = dependencies(catalog)

    class InvalidGenerator(Fixed):
        def invoke(self, payload):
            self.calls.append(deepcopy(payload))
            document = deepcopy(DOCUMENT)
            if len(self.calls) > 1:
                document["workflow"]["steps"][-1]["tools"][-1]["tool"] = "unknown_tool"
            return document

    graph = build_analysis_workflow_graph(replace(deps, workflow_agent=InvalidGenerator(DOCUMENT)),
        replace(settings(tmp_path), workflow_generation_limit=2), checkpointer=InMemorySaver())
    with pytest.raises(RuntimeError, match="workflow generation failed validation"):
        await graph.ainvoke(initial, config)
    assert catalog.registrations == []


@pytest.mark.parametrize("value", ["0", "-1", "21", "1.5", "abc", ""])
def test_invalid_generation_count(value):
    with pytest.raises(ValueError, match="WORKFLOW_GENERATION_LIMIT"):
        load_agent_settings({"WORKFLOW_GENERATION_LIMIT": value})


def test_generation_count_default_and_override():
    assert load_agent_settings({}).workflow_generation_limit == 2
    assert load_agent_settings({"WORKFLOW_GENERATION_LIMIT": "3"}).workflow_generation_limit == 3
