"""Standalone Worker entrypoint with no dependency on the source service."""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_config import load_agent_settings, require_executor_tenant
from app.agent_worker.claim_adapter import LangGraphClaimAdapter
from app.agent_worker.event_input import ExecutorApi, succeeded
from app.agent_worker.graph_provider import (
    build_agent_graph,
    build_report_agent_graph,
)
from app.agent_worker.langgraph_adapter import LangGraphEventAdapter
from app.agent_worker.worker_hooks import build_routes
from app.graphs.checkpointer_factory import create_checkpointer
from app.services.async_io_resources import close_async_io_resources
from app.services.executor_resume_persistence import persist_executor_resume
from app.worker import EventContext, ExecutorWorker, Settings
from app.worker.contracts import EventHandler


class HostSettings(BaseSettings):
    """Only the host-specific settings needed by this entrypoint."""

    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    checkpoint_database_url: str
    # Run the report agent for successful BATCH Executions of this service's
    # tenant (EXECUTOR_TENANT_ID), e.g. batches registered in pr-batch.
    report_enabled: bool = False


class DeferredHandler:
    """Break the Worker → bindings → graph → handler construction cycle."""

    def __init__(self) -> None:
        self._target: EventHandler | None = None

    def bind(self, target: EventHandler) -> None:
        if self._target is not None:
            raise RuntimeError("Handler is already bound")
        self._target = target

    async def __call__(self, context: EventContext) -> None:
        if self._target is None:
            raise RuntimeError("Agent graph handler is not initialized")
        await self._target(context)

    async def ready(self) -> bool:
        return self._target is not None


def _validate_graph(graph: Any) -> None:
    required = ("aget_state", "ainvoke")
    if not all(callable(getattr(graph, name, None)) for name in required):
        raise TypeError(
            "Graph must provide async get-state and invoke methods"
        )
    if getattr(graph, "checkpointer", None) in (None, False):
        raise ValueError("Graph must use a persistent checkpointer")


def _install_signal_handlers(worker: ExecutorWorker) -> list[signal.Signals]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, worker.request_stop)
        except NotImplementedError:
            continue
        installed.append(signum)
    return installed


async def _main() -> None:
    worker_settings = Settings()
    host_settings = HostSettings()
    analysis, report = DeferredHandler(), DeferredHandler()
    routes = build_routes(
        tenant=require_executor_tenant(load_agent_settings()),
        analysis=analysis,
        report=report if host_settings.report_enabled else None,
    )
    async with (
        ExecutorWorker(worker_settings, routes) as worker,
        create_checkpointer(host_settings.checkpoint_database_url) as checkpointer,
    ):
        analysis_graph = build_agent_graph(
            bindings=worker.bindings,
            checkpointer=checkpointer,
        )
        _validate_graph(analysis_graph)
        analysis.bind(
            LangGraphEventAdapter(
                analysis_graph, persist_result=persist_executor_resume
            )
        )
        worker.add_readiness_check("agent-graph", analysis.ready)
        if host_settings.report_enabled:
            api = ExecutorApi(worker.http)
            report_graph = build_report_agent_graph(
                checkpointer=checkpointer, executor_api=api
            )
            _validate_graph(report_graph)
            report.bind(LangGraphClaimAdapter(report_graph, accept=succeeded))
            worker.add_readiness_check("report-graph", report.ready)
        installed = _install_signal_handlers(worker)
        try:
            await worker.run()
        finally:
            loop = asyncio.get_running_loop()
            for signum in installed:
                loop.remove_signal_handler(signum)


async def main() -> None:
    try:
        await _main()
    finally:
        await close_async_io_resources()


def run_worker() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())


if __name__ == "__main__":
    run_worker()
