from uuid import uuid4

from app.graphs.node_timing import NodeTimingHandler, attach_node_timing


def test_node_timing_records_node_wall_time() -> None:
    handler = NodeTimingHandler()
    run_id = uuid4()

    handler.on_chain_start(
        {"name": "recommend_workflow"},
        {},
        run_id=run_id,
        metadata={"langgraph_node": "recommend_workflow"},
    )
    handler.on_chain_end({}, run_id=run_id)

    timing = handler.timings["recommend_workflow"]
    assert timing["count"] == 1
    assert timing["wall_seconds"] >= 0
    assert timing["avg_seconds"] >= 0
    assert timing["min_seconds"] >= 0
    assert timing["max_seconds"] >= 0


def test_node_timing_ignores_child_runnables() -> None:
    handler = NodeTimingHandler()
    run_id = uuid4()

    handler.on_chain_start(
        {"name": "llm_child"},
        {},
        run_id=run_id,
        metadata={"langgraph_node": "recommend_workflow"},
    )
    handler.on_chain_end({}, run_id=run_id)

    assert handler.timings == {}


def test_attach_node_timing_preserves_existing_callbacks() -> None:
    existing = object()

    config, handler = attach_node_timing({"callbacks": [existing]})

    assert config["callbacks"][0] is existing
    assert config["callbacks"][1] is handler
