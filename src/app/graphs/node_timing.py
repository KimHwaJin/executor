"""Record wall time spent inside each LangGraph node."""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler


class NodeTimingHandler(BaseCallbackHandler):
    """Time the node itself, not the child runnables it calls."""

    def __init__(self) -> None:
        self.timings: dict[str, dict[str, float]] = {}
        self._started: dict[UUID, tuple[str, float]] = {}

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        node = _node_name(serialized, metadata)
        if node is None:
            return
        self._started[run_id] = (node, time.perf_counter())

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish(run_id)

    def on_chain_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._finish(run_id)

    def _finish(self, run_id: UUID) -> None:
        started = self._started.pop(run_id, None)
        if started is None:
            return
        node, started_at = started
        elapsed = max(0.0, time.perf_counter() - started_at)
        bucket = self.timings.setdefault(
            node,
            {
                "wall_seconds": 0.0,
                "count": 0,
                "min_seconds": None,
                "max_seconds": 0.0,
                "avg_seconds": 0.0,
            },
        )
        bucket["wall_seconds"] = round(bucket["wall_seconds"] + elapsed, 6)
        bucket["count"] += 1
        bucket["min_seconds"] = (
            round(elapsed, 6)
            if bucket["min_seconds"] is None
            else round(min(float(bucket["min_seconds"]), elapsed), 6)
        )
        bucket["max_seconds"] = round(max(float(bucket["max_seconds"]), elapsed), 6)
        bucket["avg_seconds"] = round(
            bucket["wall_seconds"] / max(1, int(bucket["count"])),
            6,
        )


def attach_node_timing(config: dict[str, Any]) -> tuple[dict[str, Any], NodeTimingHandler]:
    handler = NodeTimingHandler()
    updated = dict(config)
    updated["callbacks"] = [*(config.get("callbacks") or []), handler]
    return updated, handler


def _node_name(
    serialized: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> str | None:
    node = str((metadata or {}).get("langgraph_node") or "")
    if node:
        return node
    return None
