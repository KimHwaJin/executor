"""Explicit, privacy-bounded OpenTelemetry tracing and W3C context propagation."""

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from executor_service.config import Settings

INSTRUMENTATION_NAME = "executor-service"
SENSITIVE_ATTRIBUTE_PARTS = (
    "code",
    "content",
    "credential",
    "database",
    "dsn",
    "header",
    "output",
    "password",
    "payload",
    "redis_url",
    "secret",
    "statement",
    "token",
)


@dataclass(frozen=True, slots=True)
class TraceCarrier:
    traceparent: str | None = None
    tracestate: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.traceparent is None

    def as_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.traceparent:
            headers["traceparent"] = self.traceparent
        if self.tracestate:
            headers["tracestate"] = self.tracestate
        return headers


class TracingManager:
    def __init__(
        self,
        settings: Settings,
        *,
        span_exporter: SpanExporter | None = None,
    ) -> None:
        self._provider: TracerProvider | None = None
        if not settings.tracing_enabled and span_exporter is None:
            self.tracer = trace.NoOpTracerProvider().get_tracer(INSTRUMENTATION_NAME)
            return
        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "openinference.project.name": settings.otel_project_name,
            }
        )
        provider = TracerProvider(
            resource=resource,
            sampler=ParentBased(TraceIdRatioBased(settings.otel_sample_ratio)),
        )
        exporter = span_exporter or OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint,
            headers=settings.otel_export_headers,
            timeout=settings.otel_exporter_timeout_seconds,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        self._provider = provider
        self.tracer = provider.get_tracer(INSTRUMENTATION_NAME)

    @contextmanager
    def span(
        self,
        name: str,
        *,
        context: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[Span]:
        safe_attributes = _safe_attributes(attributes or {})
        with self.tracer.start_as_current_span(
            name,
            context=context,
            kind=kind,
            attributes=safe_attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException as exc:
                span.set_attribute("error.type", type(exc).__name__)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise

    async def shutdown(self) -> None:
        if self._provider is not None:
            provider = self._provider
            self._provider = None
            await asyncio.to_thread(provider.shutdown)

    async def force_flush(self, timeout_millis: int = 5000) -> bool:
        if self._provider is None:
            return True
        return await asyncio.to_thread(self._provider.force_flush, timeout_millis)


def capture_trace_carrier() -> TraceCarrier:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return TraceCarrier(
        traceparent=carrier.get("traceparent"),
        tracestate=carrier.get("tracestate"),
    )


def extract_trace_context(carrier: Mapping[str, str]) -> Context:
    headers: dict[str, str] = {}
    if traceparent := carrier.get("traceparent"):
        headers["traceparent"] = traceparent
    if tracestate := carrier.get("tracestate"):
        headers["tracestate"] = tracestate
    return propagate.extract(headers)


def _safe_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in attributes.items():
        lowered = key.lower()
        if any(part in lowered for part in SENSITIVE_ATTRIBUTE_PARTS):
            continue
        if value is None or isinstance(value, (bool, int, float)):
            safe[key] = value
        elif isinstance(value, str):
            safe[key] = value[:255]
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, (bool, int, float, str)) for item in value
        ):
            safe[key] = [item[:255] if isinstance(item, str) else item for item in value]
    return safe


class TraceContextMiddleware:
    """Extract inbound W3C context and create a bounded HTTP server span."""

    def __init__(self, app: ASGIApp, tracing: TracingManager) -> None:
        self._app = app
        self._tracing = tracing

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        context = extract_trace_context(headers)
        path = str(scope.get("path", ""))
        method = str(scope.get("method", ""))
        with self._tracing.span(
            "executor.http.request",
            context=context,
            kind=SpanKind.SERVER,
            attributes={"http.request.method": method, "url.path": path},
        ) as span:

            async def traced_send(message: Message) -> None:
                if message.get("type") == "http.response.start":
                    span.set_attribute("http.response.status_code", message.get("status", 0))
                await send(message)

            await self._app(scope, receive, traced_send)
