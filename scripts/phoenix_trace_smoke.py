"""Send a cross-boundary trace to local Phoenix and verify it through the REST API."""

import asyncio
import json
import os
import time
from typing import Any
from urllib.parse import quote
from urllib.request import urlopen
from uuid import uuid4

from opentelemetry.trace import SpanKind

from executor_service.config import Settings
from executor_service.tracing import (
    TracingManager,
    capture_trace_carrier,
    extract_trace_context,
)

PHOENIX_URL = os.getenv("PHOENIX_URL", "http://127.0.0.1:6006").rstrip("/")
PROJECT_NAME = os.getenv("PHOENIX_SMOKE_PROJECT", f"executor-service-smoke-{uuid4().hex[:8]}")
EXPECTED_SPANS = {
    "agent.graph",
    "executor.mcp.execution_submit",
    "executor.outbox.publish",
    "executor.redis.consume",
    "executor.worker.execution",
    "executor.jupyter.cell.execute",
}


def _read_json(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=5) as response:
        return json.load(response)


def _get_spans() -> list[dict[str, Any]]:
    project = quote(PROJECT_NAME, safe="")
    payload = _read_json(f"{PHOENIX_URL}/v1/projects/{project}/spans?limit=1000")
    data = payload.get("data", [])
    return data if isinstance(data, list) else []


async def main() -> None:
    settings = Settings(
        _env_file=None,
        tracing_enabled=True,
        otel_project_name=PROJECT_NAME,
        otel_exporter_otlp_endpoint=f"{PHOENIX_URL}/v1/traces",
    )
    tracing = TracingManager(settings)
    try:
        with tracing.span("agent.graph"):
            with tracing.span("executor.mcp.execution_submit"):
                execution_carrier = capture_trace_carrier()

        with tracing.span(
            "executor.outbox.publish",
            context=extract_trace_context(execution_carrier.as_headers()),
            kind=SpanKind.PRODUCER,
        ):
            redis_carrier = capture_trace_carrier()

        with tracing.span(
            "executor.redis.consume",
            context=extract_trace_context(redis_carrier.as_headers()),
            kind=SpanKind.CONSUMER,
        ):
            with tracing.span("executor.worker.execution"):
                with tracing.span("executor.jupyter.cell.execute"):
                    pass

        if not await tracing.force_flush():
            raise RuntimeError("OpenTelemetry force_flush timed out")
    finally:
        await tracing.shutdown()

    deadline = time.monotonic() + 10
    spans: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        try:
            spans = _get_spans()
        except Exception:  # Phoenix may still be creating the project after ingestion.
            spans = []
        names = {str(span.get("name")) for span in spans}
        if EXPECTED_SPANS <= names:
            break
        await asyncio.sleep(0.5)

    selected = [span for span in spans if span.get("name") in EXPECTED_SPANS]
    names = {str(span.get("name")) for span in selected}
    missing = EXPECTED_SPANS - names
    if missing:
        raise RuntimeError(f"Phoenix did not return expected spans: {sorted(missing)}")

    trace_ids = {
        str(span.get("context", {}).get("trace_id"))
        for span in selected
        if isinstance(span.get("context"), dict)
    }
    if len(trace_ids) != 1:
        raise RuntimeError(f"Expected one propagated trace, found {len(trace_ids)}")

    print(
        json.dumps(
            {
                "project": PROJECT_NAME,
                "trace_id": next(iter(trace_ids)),
                "span_names": sorted(names),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
