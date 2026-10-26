"""Run/stream LangGraph while persisting emitted graph events through CRUD."""

from __future__ import annotations

from typing import Any, AsyncIterator
from dataclasses import replace
from langgraph.types import Command
from uuid import UUID

from app.services.graph_event_persistence import (
    GraphPersistenceContext,
    GraphPersistenceCursor,
    GraphPersistenceDispatcher,
)


async def _previous_state(graph, config) -> dict:
    getter = getattr(graph, "aget_state", None)
    if getter is None:
        return {}
    snapshot = await getter(config)
    values = getattr(snapshot, "values", None)
    return dict(values) if isinstance(values, dict) else {}


def _new_state_only(state: dict, previous: dict) -> dict:
    # Checkpoints restore old artifacts even for a read-only follow-up. Do not
    # project those artifacts as creations belonging to the new API run.
    view = dict(state)
    for key in ("workflow", "notebook", "executor_submit_response", "final_response"):
        if key not in previous:
            continue
        if view.get(key) == previous[key]:
            view.pop(key, None)
        else:
            # Once a node replaces/clears a restored field, a subsequent equal
            # value is a new event, including an identical answer to a new turn.
            previous.pop(key, None)
    return view


def _cursor_for_task(cursor, state, previous_task):
    if state.get("task_id") != previous_task:
        return replace(cursor, workflow_revision=0, plan_revision=0,
                       notebook_saved=False, executor_submitted=False,
                       final_response_saved=False, seen_workflow_log_keys=frozenset())
    return cursor


async def _link_graph_task(db: Any, state: Any, agent_run_id: UUID | str | None) -> None:
    if not isinstance(state, dict) or agent_run_id is None:
        return
    graph_task_id = state.get("task_id")
    if not graph_task_id:
        return
    from app.services.task_service import TaskService

    await TaskService.attach_graph_task_for_run(
        db,
        run_id=UUID(str(agent_run_id)),
        graph_task_id=UUID(str(graph_task_id)),
    )


async def astream_with_crud_message_persistence(
    graph: Any,
    graph_input: Any,
    *,
    db: Any,
    user_id: UUID,
    config: dict[str, Any],
    stream_mode: str = "values",
    dispatcher: GraphPersistenceDispatcher | None = None,
    agent_run_id: UUID | str | None = None,
    trigger_message_id: UUID | str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield graph states while persisting newly emitted graph events.

    Keep the DB session outside graph state.  SQLAlchemy sessions are not
    checkpoint-serializable, while LangGraph state is expected to remain a
    JSON/msgpack-friendly domain state.
    """

    persistence = dispatcher or GraphPersistenceDispatcher.default()
    previous = await _previous_state(graph, config)
    previous_task = previous.get("task_id")
    cursor = GraphPersistenceCursor(message_count=len(previous.get("messages") or []))
    async for state in graph.astream(
        graph_input,
        config=config,
        stream_mode=stream_mode,
    ):
        if isinstance(state, dict):
            if isinstance(graph_input, Command) or state.get("task_id") != previous.get("task_id"):
                await _link_graph_task(db, state, agent_run_id or state.get("run_id"))
            cursor = _cursor_for_task(cursor, state, previous_task)
            previous_task = state.get("task_id")
            context = GraphPersistenceContext.from_state(
                state,
                user_id=user_id,
                trigger_message_id=trigger_message_id,
                agent_run_id=agent_run_id or state.get("run_id"),
            )
            result = await persistence.persist_state_delta(
                db,
                _new_state_only(state, previous),
                cursor,
                context=context,
            )
            cursor = result.cursor
        yield state


async def ainvoke_with_crud_message_persistence(
    graph: Any,
    graph_input: Any,
    *,
    db: Any,
    user_id: UUID,
    config: dict[str, Any],
    dispatcher: GraphPersistenceDispatcher | None = None,
    agent_run_id: UUID | str | None = None,
    trigger_message_id: UUID | str | None = None,
    mock_llm_sleep_seconds: float | str | None = None,
) -> dict[str, Any]:
    """Invoke graph once and persist events from the returned state."""

    from app.agents.orchestration.mock_llm import (
        override_sleep_seconds,
        track_sleep,
    )
    from app.graphs.node_timing import attach_node_timing

    previous = await _previous_state(graph, config)
    timed_config, node_timing = attach_node_timing(config)
    with track_sleep() as slept:
        with override_sleep_seconds(
            None
            if mock_llm_sleep_seconds is None
            else float(mock_llm_sleep_seconds)
        ):
            state = await graph.ainvoke(graph_input, config=timed_config)
    if isinstance(state, dict):
        state["mock_llm_sleep_seconds"] = slept[0]
        state["node_timings"] = node_timing.timings
        if isinstance(graph_input, Command) or state.get("task_id") != previous.get("task_id"):
            await _link_graph_task(db, state, agent_run_id or state.get("run_id"))
        persistence = dispatcher or GraphPersistenceDispatcher.default()
        context = GraphPersistenceContext.from_state(
            state,
            user_id=user_id,
            trigger_message_id=trigger_message_id,
            agent_run_id=agent_run_id or state.get("run_id"),
        )
        if len(state.get("messages") or []) > len(previous.get("messages") or []):
            previous.pop("final_response", None)
        await persistence.persist_state_delta(
            db,
            _new_state_only(state, previous),
            GraphPersistenceCursor(message_count=len(previous.get("messages") or [])),
            context=context,
        )
    return state


__all__ = [
    "ainvoke_with_crud_message_persistence",
    "astream_with_crud_message_persistence",
]
