"""Prometheus metrics shared by interfaces and background workers."""

from prometheus_client import Counter, Gauge

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
OUTBOX_PENDING = Gauge(
    "executor_outbox_pending_events",
    "Transactional Outbox events currently waiting for Redis publication.",
)
OUTBOX_OLDEST_PENDING_AGE = Gauge(
    "executor_outbox_oldest_pending_age_seconds",
    "Age in seconds of the oldest pending Transactional Outbox event.",
)
STREAM_MESSAGES = Counter(
    "executor_stream_messages_total",
    "Redis Stream messages by Executor consumer outcome.",
    labelnames=("outcome",),
)
STREAM_RECLAIMED = Counter(
    "executor_stream_reclaimed_messages_total",
    "Stale Redis pending messages reclaimed by a live Executor consumer.",
)
STREAM_DEAD_LETTERED = Counter(
    "executor_stream_dead_lettered_messages_total",
    "Malformed or unsupported Redis Stream messages moved to the DLQ.",
    labelnames=("reason",),
)
STREAM_PENDING = Gauge(
    "executor_stream_pending_messages",
    "Messages in the Executor Redis consumer group's pending entries list.",
)
STREAM_LAG = Gauge(
    "executor_stream_consumer_lag",
    "Undelivered Redis Stream messages reported for the Executor consumer group.",
)
WORKER_ACTIVE_JOBS = Gauge(
    "executor_worker_active_jobs",
    "Execution jobs currently dispatched in this Executor process.",
)
JUPYTER_POOL_SERVERS = Gauge(
    "executor_jupyter_pool_servers",
    "Enabled Jupyter servers by scheduling pool and operational status.",
    labelnames=("pool", "status"),
)
JUPYTER_POOL_CAPACITY = Gauge(
    "executor_jupyter_pool_capacity",
    "Total schedulable Jupyter execution capacity by pool.",
    labelnames=("pool",),
)
JUPYTER_POOL_CAPACITY_USED = Gauge(
    "executor_jupyter_pool_capacity_used",
    "Running, waiting, and retained-kernel capacity used by Jupyter pool.",
    labelnames=("pool",),
)
JUPYTER_POOL_QUEUED = Gauge(
    "executor_jupyter_pool_queued_executions",
    "Queued Executions requesting each Jupyter scheduling pool.",
    labelnames=("pool",),
)
