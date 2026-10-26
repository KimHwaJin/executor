"""API-boundary LangGraph runtime.

Graph nodes stay free of DB/CRUD calls. API handlers build graph_input here,
then persist UI messages through ``ainvoke_with_crud_message_persistence``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
import logging
import os
from typing import Any, Mapping
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from agent_config import build_langgraph_thread_id, load_agent_settings
from config import settings
from app.core.enums import AgentRunStatus
from app.services.graph_crud_persistence import (
    ainvoke_with_crud_message_persistence,
    astream_with_crud_message_persistence,
)
from app.services.graph_event_persistence import GraphPersistenceDispatcher


GRAPH_MESSAGE_SOURCE = "dtest-agent"
logger = logging.getLogger(__name__)


class AgentGraphRuntime:
    """Build LangGraph instances for cached tests and per-call API execution."""

    def __init__(self) -> None:
        self._graph: Any | None = None
        self._stack: AsyncExitStack | None = None
        self._mock_graph: Any | None = None
        self._mock_stack: AsyncExitStack | None = None
        self._init_lock = asyncio.Lock()

    def override_graph(self, graph: Any | None) -> None:
        if self._stack is not None:
            raise RuntimeError("await runtime.shutdown() before overriding the graph")
        self._graph = graph


    def _worker_settings(self, WorkerSettings: type[Any]) -> Any:
        try:
            return WorkerSettings()
        except ValidationError:
            return WorkerSettings(
                database_url=settings.database_url.replace("+asyncpg", ""),
                redis_url=settings.redis_url,
                namespace="dtest-agent",
            )

    def _load_graph_inputs(self, *, mock_llm: bool = False) -> tuple[Any, Any, str]:
        try:
            from app.agents.orchestration.dependencies import create_llm_dependencies
            from app.agents.orchestration.mock_llm import create_mock_dependencies
        except ImportError as exc:
            raise RuntimeError(
                f"Agent graph dependencies are not available: {exc}"
            ) from exc

        agent_settings = load_agent_settings()
        dependencies = (
            create_mock_dependencies()
            if mock_llm
            else create_llm_dependencies(agent_settings)
        )
        checkpointer = (settings.graph_checkpointer or "postgres").strip().lower()
        return dependencies, agent_settings, checkpointer

    @asynccontextmanager
    async def _graph_context(self, *, mock_llm: bool = False) -> AsyncIterator[Any]:
        dependencies, agent_settings, checkpointer = self._load_graph_inputs(
            mock_llm=mock_llm
        )
        try:
            from langgraph.checkpoint.memory import InMemorySaver

            from app.agent_worker.api_bridge import ApiWorkerBridge
            from app.graphs.builders.build_analysis_workflow_graph import (
                build_analysis_workflow_graph,
            )
            from app.graphs.checkpointer_factory import create_checkpointer
            from app.services.workflow_persistence import workflow_store_from_environment
            from app.worker import Settings as WorkerSettings
        except ImportError as exc:
            raise RuntimeError(
                f"Agent graph dependencies are not available: {exc}"
            ) from exc

        if checkpointer == "postgres":
            async with AsyncExitStack() as stack:
                try:
                    worker_bridge = await stack.enter_async_context(
                        ApiWorkerBridge(self._worker_settings(WorkerSettings))
                    )
                except Exception:
                    logger.exception(
                        "connection_open_failed component=worker_postgres_bridge"
                    )
                    raise
                try:
                    graph_checkpointer = await stack.enter_async_context(
                        create_checkpointer(
                            database_url=agent_settings.checkpoint_db_uri,
                            setup_on_start=agent_settings.checkpoint_setup_on_start,
                        )
                    )
                except Exception:
                    logger.exception(
                        "connection_open_failed component=checkpoint_postgres"
                    )
                    raise
                yield build_analysis_workflow_graph(
                    dependencies,
                    agent_settings,
                    checkpointer=graph_checkpointer,
                    bindings=worker_bridge.bindings,
                    workflow_store=workflow_store_from_environment(),
                )
            return

        if checkpointer == "memory":
            yield build_analysis_workflow_graph(
                dependencies,
                agent_settings,
                checkpointer=InMemorySaver(),
                workflow_store=workflow_store_from_environment(),
            )
            return

        raise RuntimeError(
            "GRAPH_CHECKPOINTER must be 'memory' or 'postgres', "
            f"got {checkpointer!r}"
        )

    @asynccontextmanager
    async def open_graph(self, *, mock_llm: bool = False) -> AsyncIterator[Any]:
        if not mock_llm and self._graph is not None and self._stack is None:
            yield self._graph
            return
        if mock_llm and self._mock_graph is not None and self._mock_stack is None:
            yield self._mock_graph
            return
        async with self._init_lock:
            if mock_llm:
                if self._mock_stack is not None:
                    await self._mock_stack.aclose()
                self._mock_stack = None
                self._mock_graph = None
            else:
                if self._stack is not None:
                    await self._stack.aclose()
                self._stack = None
                self._graph = None
        try:
            async with self._graph_context(mock_llm=mock_llm) as graph:
                yield graph
        except Exception:
            logger.exception("agent_graph_invocation_failed component=run_execution")
            raise

    async def get_graph(self, *, mock_llm: bool = False) -> Any:
        if mock_llm:
            return await self._get_cached_graph(mock_llm=True)
        return await self._get_cached_graph(mock_llm=False)

    async def _get_cached_graph(self, *, mock_llm: bool) -> Any:
        graph_attr = "_mock_graph" if mock_llm else "_graph"
        stack_attr = "_mock_stack" if mock_llm else "_stack"
        graph = getattr(self, graph_attr)
        if graph is not None:
            return graph
        async with self._init_lock:
            graph = getattr(self, graph_attr)
            if graph is not None:
                return graph

            stack = AsyncExitStack()
            await stack.__aenter__()
            try:
                graph_context = self._graph_context(mock_llm=mock_llm)
                graph = await stack.enter_async_context(graph_context)
            except BaseException:
                await stack.aclose()
                setattr(self, graph_attr, None)
                raise
            setattr(self, stack_attr, stack)
            setattr(self, graph_attr, graph)
            return graph

    async def get_real_graph(self) -> Any:
        if self._graph is not None:
            return self._graph
        async with self._init_lock:
            if self._graph is not None:
                return self._graph

            stack = AsyncExitStack()
            await stack.__aenter__()
            try:
                graph_context = self._graph_context()
                self._graph = await stack.enter_async_context(graph_context)
            except BaseException:
                await stack.aclose()
                self._graph = None
                raise
            self._stack = stack
            return self._graph

    async def shutdown(self) -> None:
        async with self._init_lock:
            if self._stack is not None:
                await self._stack.aclose()
            if self._mock_stack is not None:
                await self._mock_stack.aclose()
            self._stack = None
            self._graph = None
            self._mock_stack = None
            self._mock_graph = None


runtime = AgentGraphRuntime()


def is_graph_persisted_message(metadata: Mapping[str, Any] | None) -> bool:
    return bool(metadata) and metadata.get("source") == GRAPH_MESSAGE_SOURCE


def user_request_from_messages(messages: list[dict[str, Any]]) -> str:
    for item in reversed(messages):
        if not isinstance(item, dict):
            continue
        if item.get("role") not in {"user", "human"}:
            continue
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            ]
            text = "".join(parts).strip()
            if text:
                return text
    raise HTTPException(
        status_code=422,
        detail="Run input must include a user message with text content.",
    )


def interrupt_payload(state: Mapping[str, Any] | None) -> list[dict[str, Any]] | None:
    if not state:
        return None
    interrupts = state.get("__interrupt__") or []
    if not interrupts:
        return None
    payload: list[dict[str, Any]] = []
    for item in interrupts:
        value = getattr(item, "value", item)
        if isinstance(value, dict):
            payload.append(value)
        else:
            payload.append({"value": value})
    return payload or None


def run_status_from_state(state: Mapping[str, Any] | None) -> AgentRunStatus:
    if interrupt_payload(state):
        return AgentRunStatus.INTERRUPTED
    return AgentRunStatus.SUCCESS


def build_graph_input(
    *,
    user_id: UUID,
    project_id: UUID,
    session_id: UUID,
    run_id: UUID | str,
    user_request: str,
    trigger_message_id: UUID | str | None = None,
    request_id: str | None = None,
) -> dict[str, str]:
    run_key = str(run_id)
    thread_id = build_langgraph_thread_id(str(session_id))
    graph_input = {
        "user_id": str(user_id),
        "project_id": str(project_id),
        "session_id": str(session_id),
        "run_id": run_key,
        "thread_id": thread_id,
        "request_id": request_id or str(uuid4()),
        "user_request": user_request,
        "agent_run_id": run_key,
    }
    if trigger_message_id is not None:
        graph_input["trigger_message_id"] = str(trigger_message_id)
    return graph_input


def graph_config(
    session_id: UUID | str,
    run_id: UUID | str,
    callbacks: list[Any] | None = None,
) -> dict[str, Any]:
    thread_id = build_langgraph_thread_id(str(session_id))
    config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
    if callbacks:
        config["callbacks"] = callbacks
    return config


async def ainvoke_user_turn(
    db: Any,
    *,
    user_id: UUID,
    project_id: UUID,
    session_id: UUID,
    run_id: UUID,
    user_request: str,
    trigger_message_id: UUID | str | None = None,
    request_id: str | None = None,
    dispatcher: GraphPersistenceDispatcher | None = None,
    graph: Any | None = None,
    callbacks: list[Any] | None = None,
    mock_llm: bool = False,
    mock_llm_sleep_seconds: float | str | None = None,
) -> dict[str, Any]:
    graph_input = build_graph_input(
        user_id=user_id,
        project_id=project_id,
        session_id=session_id,
        run_id=run_id,
        user_request=user_request,
        trigger_message_id=trigger_message_id,
        request_id=request_id,
    )
    # Explicitly reset this channel on a new user turn in a reused session.
    if graph is None:
        compiled = await runtime.get_graph(mock_llm=mock_llm)
        return await ainvoke_with_crud_message_persistence(
            compiled,
            graph_input,
            db=db,
            user_id=user_id,
            config=graph_config(session_id, run_id, callbacks),
            dispatcher=dispatcher,
            agent_run_id=run_id,
            trigger_message_id=trigger_message_id,
            mock_llm_sleep_seconds=mock_llm_sleep_seconds,
        )
    return await ainvoke_with_crud_message_persistence(
        graph,
        graph_input,
        db=db,
        user_id=user_id,
        config=graph_config(session_id, run_id, callbacks),
        dispatcher=dispatcher,
        agent_run_id=run_id,
        trigger_message_id=trigger_message_id,
        mock_llm_sleep_seconds=mock_llm_sleep_seconds,
    )


async def ainvoke_resume(
    db: Any,
    *,
    user_id: UUID,
    session_id: UUID,
    checkpoint_run_id: UUID,
    agent_run_id: UUID | None = None,
    command: dict[str, Any] | str,
    dispatcher: GraphPersistenceDispatcher | None = None,
    graph: Any | None = None,
    callbacks: list[Any] | None = None,
    mock_llm: bool = False,
    mock_llm_sleep_seconds: float | str | None = None,
) -> dict[str, Any]:
    from langgraph.types import Command

    graph_input = Command(resume=command)
    config = graph_config(session_id, checkpoint_run_id, callbacks)
    run_id = agent_run_id or checkpoint_run_id
    if graph is None:
        compiled = await runtime.get_graph(mock_llm=mock_llm)
        return await ainvoke_with_crud_message_persistence(
            compiled,
            graph_input,
            db=db,
            user_id=user_id,
            config=config,
            dispatcher=dispatcher,
            agent_run_id=run_id,
            mock_llm_sleep_seconds=mock_llm_sleep_seconds,
        )
    return await ainvoke_with_crud_message_persistence(
        graph,
        graph_input,
        db=db,
        user_id=user_id,
        config=config,
        dispatcher=dispatcher,
        agent_run_id=run_id,
        mock_llm_sleep_seconds=mock_llm_sleep_seconds,
    )


async def astream_user_turn(
    db: Any,
    *,
    user_id: UUID,
    project_id: UUID,
    session_id: UUID,
    run_id: UUID,
    user_request: str,
    trigger_message_id: UUID | str | None = None,
    request_id: str | None = None,
    dispatcher: GraphPersistenceDispatcher | None = None,
    graph: Any | None = None,
):
    graph_input = build_graph_input(
        user_id=user_id,
        project_id=project_id,
        session_id=session_id,
        run_id=run_id,
        user_request=user_request,
        trigger_message_id=trigger_message_id,
        request_id=request_id,
    )
    if graph is None:
        async with runtime.open_graph() as compiled:
            async for state in astream_with_crud_message_persistence(
                compiled,
                graph_input,
                db=db,
                user_id=user_id,
                config=graph_config(session_id, run_id),
                dispatcher=dispatcher,
                agent_run_id=run_id,
                trigger_message_id=trigger_message_id,
            ):
                yield state
            return
    async for state in astream_with_crud_message_persistence(
        graph,
        graph_input,
        db=db,
        user_id=user_id,
        config=graph_config(session_id, run_id),
        dispatcher=dispatcher,
        agent_run_id=run_id,
        trigger_message_id=trigger_message_id,
    ):
        yield state


__all__ = [
    "GRAPH_MESSAGE_SOURCE",
    "AgentGraphRuntime",
    "ainvoke_resume",
    "ainvoke_user_turn",
    "astream_user_turn",
    "build_graph_input",
    "graph_config",
    "interrupt_payload",
    "is_graph_persisted_message",
    "run_status_from_state",
    "runtime",
    "user_request_from_messages",
]
