"""Prometheus metrics shared by interfaces and background workers."""

from prometheus_client import Counter

MCP_TOOL_CALLS = Counter(
    "executor_mcp_tool_calls_total",
    "MCP tool calls by tool and outcome.",
    labelnames=("tool", "outcome"),
)
EXECUTIONS_SUBMITTED = Counter(
    "executor_executions_submitted_total",
    "New executions persisted by the executor.",
)
OUTBOX_PUBLISHED = Counter(
    "executor_outbox_published_total",
    "Outbox events published to Redis Streams.",
)
OUTBOX_FAILURES = Counter(
    "executor_outbox_publish_failures_total",
    "Outbox publish attempts that failed.",
)
