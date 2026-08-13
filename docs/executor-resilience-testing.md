# Executor Resilience Testing

These local E2E scenarios start real Executor processes against the Docker Compose PostgreSQL,
Redis, shared PV, and Jupyter fleet. They verify process and dependency failure behavior that unit
tests cannot cover.

## Prerequisites

```bash
docker compose --profile multi-jupyter --profile batch-jupyter up -d --wait
uv run alembic upgrade head
```

The scripts select free loopback ports automatically. Set `DRAIN_SMOKE_PRIMARY_PORT`,
`DRAIN_SMOKE_SECONDARY_PORT`, `LOAD_SMOKE_PRIMARY_PORT`, `LOAD_SMOKE_SECONDARY_PORT`,
`REDIS_OUTAGE_SMOKE_PORT`, or `JUPYTER_OUTAGE_SMOKE_PORT` only when fixed ports are required.

## Automated scenarios

### STATIC execution observability

```bash
uv run python scripts/static_execution_observability_smoke.py
```

This non-disruptive scenario uses the already running Executor stack. It submits one two-Step
STATIC execution through REST and one through MCP, then cross-checks the public history APIs,
PostgreSQL, Redis Stream, shared PV, and Runtime Target probe. Each execution must:

- expose `QUEUED -> RUNNING -> SUCCEEDED`;
- persist two successful current Steps, one successful Attempt, and two immutable Step Attempts;
- read current Steps, Attempt detail, and Step Attempt history through their separate paginated
  REST/MCP operations;
- publish every PostgreSQL Outbox row and expose the same `event_id` values in Redis;
- register the generated text Artifact and final `execution.ipynb`;
- retain the historical Runtime session ID on the Attempt while clearing it from the terminal
  Execution; and
- leave no active execution or Runtime session after both cases finish.

The default profile is `basic`. Override `OBSERVABILITY_RUNTIME_PROFILE`,
`OBSERVABILITY_TIMEOUT_SECONDS`, or `OBSERVABILITY_STREAM_SCAN_LIMIT` when the local topology
requires it. Run this against an otherwise idle local Runtime Target because the final session
leak assertion expects the probed target to have no unrelated sessions.

### STATIC failure, retained-kernel retry, and running cancellation

```bash
uv run python scripts/static_failure_retry_cancel_e2e.py
```

This non-disruptive scenario covers both abnormal STATIC lifecycle paths against the running
Compose stack. Failure and retry use MCP; cancellation uses REST. For each Execution it
cross-checks the public current/history responses, PostgreSQL rows, Transactional Outbox, Redis
Stream event IDs and v1 payloads, shared-PV Artifact evidence, and the exact Jupyter session.

The retry case fails after writing an Artifact, verifies `FROM_FAILED_STEP` preserves the original
target and kernel, then resumes from the failed Step and deletes that kernel after success. The
failed write remains `INCOMPLETE`, the successful retry write is `AVAILABLE`, Attempt/Step Attempt
history stays immutable, and the completed notebook is registered.

The cancellation case writes a marker before entering a long cell, requests cancellation while
the kernel is running, and requires Execution, Attempt, current Steps, and the running Step Attempt
to become `CANCELLED`. The kernel must be deleted, the marker must be preserved as `INCOMPLETE`,
later Steps must not run, and no successful notebook Artifact may be registered.

Override `STATIC_LIFECYCLE_RUNTIME_PROFILE`, `STATIC_LIFECYCLE_TIMEOUT_SECONDS`, or
`STATIC_LIFECYCLE_STREAM_SCAN_LIMIT` when the local topology requires it. The host-side session
probe defaults to `JUPYTER_ENDPOINT`; set `STATIC_LIFECYCLE_JUPYTER_ENDPOINT` when the registered
Target needs a different host-accessible endpoint than its container-internal address.

### DYNAMIC correction, finish, and running cancellation

```bash
uv run python scripts/dynamic_execution_lifecycle_e2e.py
```

This non-disruptive scenario alternates REST and MCP commands while checking PostgreSQL, Outbox,
Redis Streams, shared-PV Artifacts, the generated notebook, and the exact Jupyter session. Its
normal flow submits one DYNAMIC cell, appends a successful cell, appends an expected failing cell,
adds a corrected follow-up cell, and finishes. Every cell must use the same target, kernel, and
Attempt; the failed cell remains immutable rather than being rerun or replaced.

The second flow cancels a running DYNAMIC cell after it writes a marker. The interrupted execution
job preserves the marker as an `INCOMPLETE` Artifact, while the replacement cancellation job is
the single owner of kernel cleanup and the `CANCELLED` state/event. This prevents competing cleanup
operations from incorrectly reporting a successfully removed kernel as a cleanup failure.

Override `DYNAMIC_LIFECYCLE_RUNTIME_PROFILE`, `DYNAMIC_LIFECYCLE_TIMEOUT_SECONDS`, or
`DYNAMIC_LIFECYCLE_STREAM_SCAN_LIMIT` as needed. Set
`DYNAMIC_LIFECYCLE_JUPYTER_ENDPOINT` when the host-side session probe cannot use the target's
container-internal endpoint.

### Graceful drain and handoff

```bash
uv run python scripts/multi_executor_drain_smoke.py
```

The scenario starts two Executor processes and two one-capacity INTERACTIVE Jupyter servers. The
primary receives a short execution, a long execution, and one queued execution before SIGTERM.
It verifies that:

- the short execution succeeds within the drain window;
- the long execution exceeds the window and becomes `FAILED/WORKER_SHUTDOWN/FROM_START`;
- the long execution kernel cleanup succeeds;
- the queued execution is claimed exactly once by the secondary Executor.

### Concurrent load and capacity

```bash
uv run python scripts/multi_executor_load_smoke.py
```

The default run submits 30 STATIC executions across the INTERACTIVE and BATCH pools, with two
Executor processes and four one-capacity Jupyter servers. It requires all executions to succeed
with exactly one Attempt, both Executor consumers to own work, every server peak to remain within
capacity, and zero active kernels after completion.

Use `RESILIENCE_EXECUTION_COUNT` for 20-60 executions and
`RESILIENCE_CELL_SLEEP_SECONDS` to adjust overlap duration.

### Redis pause and Outbox recovery

```bash
uv run python scripts/executor_redis_outage_smoke.py
```

This uses Redis `CLIENT PAUSE ... ALL` for eight seconds. The command affects the local Redis
instance, so do not run it against a shared or production Redis. The submitted execution must
finish while Redis is paused through PostgreSQL reconciliation. After Redis resumes, every durable
Outbox event must become `PUBLISHED` and appear in the configured Stream.

Set `REDIS_OUTAGE_PAUSE_MILLISECONDS` to a value of at least 5000 when a longer pause is needed.

### Jupyter OFFLINE and recovery

```bash
ALLOW_DOCKER_JUPYTER_OUTAGE_TEST=1 \
  uv run python scripts/jupyter_server_outage_smoke.py
```

The explicit opt-in is required because the script temporarily stops the local
`jupyter-secondary` container. It always brings the container back in `finally`. The test probes
the stopped server as `OFFLINE`, verifies that work uses the healthy primary, restores and probes
the secondary as `ACTIVE`, and then verifies that both servers receive concurrent work.

For a retained Tool-error kernel, run:

```bash
uv run python scripts/jupyter_retry_offline_recovery_smoke.py
```

This test makes the registered endpoint temporarily unreachable without deleting the live kernel,
requests retry, verifies the Execution remains `QUEUED` on the original server and kernel, restores
the endpoint, and confirms execution resumes from the failed Step.

### Forced process loss

```bash
uv run python scripts/multi_executor_failover_smoke.py
```

This existing scenario sends SIGKILL to the primary Executor. The surviving process must classify
the expired lease as `LEASE_EXPIRED`, delete the abandoned kernel, and complete exactly one
explicit `FROM_START` retry.

## Cleanup and retained evidence

Each scenario uses a unique Redis Stream and deletes it on exit. Set
`RESILIENCE_KEEP_STREAMS=true` to retain the primary and DLQ Streams for diagnosis. PostgreSQL
Execution, Attempt, Step, Artifact, and Outbox rows and execution-scoped PV files are intentionally
retained as audit evidence.

Useful read-only PostgreSQL checks are:

```sql
SELECT id, status, failure_type, retry_strategy, lease_owner,
       runtime_target_id, runtime_session_id, created_at, finished_at
FROM executions
WHERE user_id = 'resilience-user'
ORDER BY created_at DESC;

SELECT execution_id, attempt_number, status, lease_owner,
       runtime_target_id, runtime_session_id, failure_type, runtime_session_cleanup_status
FROM execution_attempts
WHERE execution_id IN (
    SELECT id FROM executions
    WHERE user_id = 'resilience-user'
)
ORDER BY started_at DESC;

SELECT aggregate_id AS execution_id, event_type, status,
       attempt_count, created_at, published_at, last_error
FROM outbox_events
WHERE aggregate_id IN (
    SELECT id FROM executions
    WHERE user_id = 'resilience-user'
)
ORDER BY created_at DESC;
```

For a retained Stream, inspect delivery state with:

```bash
redis-cli XINFO GROUPS <stream-name>
redis-cli XPENDING <stream-name> <consumer-group>
redis-cli XRANGE <stream-name> - +
```

## Validated baseline

The local baseline validated on 2026-08-09 produced:

- graceful drain: short `SUCCEEDED`, long `WORKER_SHUTDOWN` with cleanup `SUCCEEDED`, queued work
  owned by the secondary;
- load: 30/30 `SUCCEEDED`, two distinct Executor owners, peak one execution per one-capacity
  Jupyter server, zero leaked kernels;
- Redis pause: execution completed during the pause and all four Outbox events published after
  recovery;
- Jupyter outage: `OFFLINE` avoidance succeeded and the restored server returned to scheduling.
