# Executor Service

Asynchronous Jupyter execution control plane exposed through MCP 2026-07-28 Streamable HTTP and a
versioned REST API. PostgreSQL is the source of truth. MCP Tool and REST calls persist work and
return immediately while a Redis consumer worker executes STATIC plans or one-cell-at-a-time
DYNAMIC plans in Jupyter.

## Implemented scope

- Official MCP Python SDK 2.x `MCPServer`, exposed at `POST /mcp`
- REST execution facade, OpenAPI, Swagger UI, and ReDoc under `/api/v1`, `/openapi.json`, `/docs`,
  and `/redoc`
- Execution tools: `executor_get_capabilities`, `execution_submit`, `execution_get`,
  `execution_cancel`, `execution_retry`, `execution_continue`, `execution_finish`,
  `execution_list`, `execution_step_list`, `execution_attempt_list`,
  `execution_event_list`, `execution_trace_get`,
  `execution_artifact_list`, `execution_artifact_get`
- Jupyter fleet tools: `jupyter_server_upsert`, `jupyter_server_list`,
  `jupyter_server_get`, `jupyter_server_probe`, `jupyter_server_remove`,
  `jupyter_server_set_state`
- Execution, ExecutionStep, and OutboxEvent persistence with SQLAlchemy 2 and Alembic
- Transactional Outbox publisher with at-least-once Redis Stream delivery
- Redis consumer group worker with PostgreSQL reconciliation, stale Pending recovery, and DLQ
- Multi-Executor coordination through PostgreSQL row locks, unique lease owners, and crash recovery
- Jupyter REST/WebSocket kernel execution, interrupt, and deletion
- Multi-server Jupyter registry, encrypted credentials, health probes, capacity scheduling,
  execution attempts, leases, and heartbeats
- Strict INTERACTIVE/BATCH Jupyter scheduling isolation with a two-server local BATCH topology
- Safe server draining and retained-kernel retry from a failed Step
- Classified Tool/infrastructure failures, graceful Worker shutdown, and FROM_START recovery
- Append-only DYNAMIC cells with optimistic version checks and same-kernel continuation
- Dynamic wait/total runtime deadlines, retained-kernel audits, and orphan cleanup
- Immutable per-Attempt Step history and an end-to-end execution event trace
- Automatic and Manifest-based Artifact registration with checksum and lineage
- Durable `.ipynb` output and execution-scoped artifact directories on the shared PV
- W3C trace-context propagation across HTTP/MCP, PostgreSQL Outbox, Redis Streams, Worker,
  and Jupyter operations with optional OTLP export to Arize Phoenix
- `/healthz` and `/readyz` operational endpoints
- PostgreSQL, Redis, INTERACTIVE/BATCH `jupyter/datascience-notebook` fleets, and opt-in Phoenix
  through Docker Compose

MCP Tasks are deliberately not used. `execution_submit` returns an `execution_id` while the
execution starts as `QUEUED`. Poll with `execution_get` or request cancellation with
`execution_cancel`. DYNAMIC execution accepts one initial cell, returns
`WAITING_FOR_NEXT_STEP` after success or code error, and then accepts exactly one append-only cell
through `execution_continue`. `execution_finish` persists the final notebook and deletes the
retained kernel. MCP Tasks are not required for this lifecycle.

## Deferred decisions

Return-value materialization, reusable Asset promotion, and user-versus-project Asset visibility
are intentionally not implemented yet. Their agreed constraints, open questions, and resume
criteria are tracked in [Deferred Decisions](docs/deferred-decisions.md). Update that decision log
before implementing or changing any deferred behavior.

## Local setup

Requirements: uv, Docker, and Docker Compose. uv installs the pinned CPython 3.12 runtime when it
is not already present.

```bash
cp .env.example .env
docker compose up -d
uv sync --dev
uv run alembic upgrade head
uv run executor-service
```

Native Windows is supported. Alembic and the `executor-service` console entry point run psycopg
on a `SelectorEventLoop`, because psycopg async connections are incompatible with Windows'
default `ProactorEventLoop`. Use `uv run executor-service` instead of invoking Uvicorn directly so
the compatible loop runner is applied.

To run a second Jupyter server against the same local PV:

```bash
docker compose --profile multi-jupyter up -d --wait
```

To run the two-server BATCH pool on ports `8890` and `8891`, or the full local four-server fleet:

```bash
docker compose --profile batch-jupyter up -d --wait
docker compose --profile multi-jupyter --profile batch-jupyter up -d --wait
```

Operational endpoints:

- MCP: `http://127.0.0.1:8000/mcp`
- REST API: `http://127.0.0.1:8000/api/v1`
- Swagger UI: `http://127.0.0.1:8000/docs`
- OpenAPI: `http://127.0.0.1:8000/openapi.json`
- liveness: `http://127.0.0.1:8000/healthz`
- readiness (PostgreSQL, Redis, Jupyter, Worker admission): `http://127.0.0.1:8000/readyz`
- Worker lifecycle and active execution count: `http://127.0.0.1:8000/workerz`

### Phoenix tracing

Tracing is disabled by default and is not a readiness dependency. To run the validated local
Phoenix image and enable OTLP/HTTP export:

```bash
docker compose --profile observability up -d --wait phoenix
TRACING_ENABLED=true uv run executor-service
```

Phoenix UI is available at `http://127.0.0.1:6006`. The default collector endpoint is
`http://127.0.0.1:6006/v1/traces`; configure `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_PROJECT_NAME`, `OTEL_SAMPLE_RATIO`, and optional `OTEL_EXPORTER_OTLP_HEADERS` for another
deployment. Header values are secret settings and are never logged.

The Agent should send W3C `traceparent` and optional `tracestate` headers on `/mcp`. Executor
persists that context on the Execution and Outbox Event, creates a producer span when publishing,
adds the current context to Redis Stream fields, and resumes it in the consumer Worker. PostgreSQL
reconciliation uses the Execution's last command context when the Redis event is unavailable.
Generated code, cell output, dataset content, query text, credentials, and tokens are deliberately
excluded from span attributes. OTLP export failure does not change Execution state.

Run the local collector verification after Phoenix is healthy:

```bash
uv run python scripts/phoenix_trace_smoke.py
```

Run the official SDK client smoke test in a second terminal:

```bash
uv run python scripts/mcp_smoke.py
uv run python scripts/jupyter_gateway_smoke.py
uv run python scripts/jupyter_execution_smoke.py
uv run python scripts/jupyter_dynamic_smoke.py
uv run python scripts/jupyter_dynamic_lifecycle_smoke.py
uv run python scripts/jupyter_cancel_smoke.py
uv run python scripts/jupyter_failure_smoke.py
uv run python scripts/jupyter_fleet_smoke.py
uv run python scripts/jupyter_retry_smoke.py
uv run python scripts/jupyter_worker_recovery_smoke.py
uv run python scripts/jupyter_drain_smoke.py
uv run python scripts/jupyter_artifact_smoke.py
uv run python scripts/jupyter_batch_pool_smoke.py
uv run python scripts/multi_executor_failover_smoke.py
uv run python scripts/multi_executor_drain_smoke.py
uv run python scripts/multi_executor_load_smoke.py
uv run python scripts/executor_redis_outage_smoke.py
ALLOW_DOCKER_JUPYTER_OUTAGE_TEST=1 uv run python scripts/jupyter_server_outage_smoke.py
uv run python scripts/phoenix_trace_smoke.py
```

The resilience scenarios and their safety boundaries are documented in
[Executor Resilience Testing](docs/executor-resilience-testing.md).

## Quality checks

```bash
uv run ruff check .
uv run ty check
uv run pytest
```

Run the opt-in real PostgreSQL concurrency suite while the local Compose PostgreSQL is healthy:

```bash
EXECUTOR_RUN_POSTGRES_TESTS=1 uv run pytest tests/test_multi_worker_postgres.py
```

Migration checks:

```bash
uv run alembic current
uv run alembic downgrade base
uv run alembic upgrade head
```

## Tool contracts

The same execution lifecycle is available as REST without internally calling MCP. REST requests
and responses have transport-specific DTOs but map to the same application commands, PostgreSQL
transactions, Outbox, and Worker. See [Executor REST API v1](docs/rest-api.md) for every endpoint
and runnable curl examples.

`execution_submit` accepts one `request` object. Important fields are:

- `idempotency_key`: required for safe retries; reuse with different content is rejected
- `mode`: `STATIC` or `DYNAMIC`
- `trigger_type`: `INTERACTIVE` or `BATCH`
- `kernel_name`: one of the deployment's configured kernels
- `source`: either an INLINE ExecutionSpec or a shared-PV PATH plus SHA-256
- `context`: Agent-owned user/project/session/Task IDs; Executor creates `execution_id`
- `actor`: required audit principal with type `USER` or `BATCH` and a stable upstream ID

Every public mutation records `created_by`/`updated_by` attribution on the affected Execution,
Step, Attempt, Artifact, Outbox Event, or Jupyter server where applicable. Interactive submits
require a `USER` actor and batch submits require a `BATCH` actor. Additional autonomous actor types
remain deferred in [Deferred Decisions](docs/deferred-decisions.md#dd-003-additional-audit-actor-types).

`jupyter_pool` is not accepted from callers. Executor derives `INTERACTIVE` or `BATCH` from
`trigger_type`, then selects a healthy compatible server with available capacity inside that pool.

INLINE and PATH resolve to the same versioned ExecutionSpec. INLINE embeds `source.spec`; PATH
references a UTF-8 JSON file under the shared PV root using a relative path and required SHA-256.
The spec owns `execution_plan_id` and ordered Steps containing `plan_step_id`, code, and optional
Skill/Tool metadata. Executor persists the normalized source and creates one ExecutionStep and one
Notebook code cell per spec Step. See [ExecutionSpec v1](docs/execution-spec.md).

For `DYNAMIC`, submit exactly one ExecutionSpec Step with sequence `0`. After the execution reaches
`WAITING_FOR_NEXT_STEP`, call `execution_continue` with the current `version`, a new idempotency
key, and an INLINE or PATH ExecutionSpec containing exactly the next consecutive Step. A stale
version or non-consecutive sequence is rejected. Each Step records its Agent-owned
`execution_plan_id` and `plan_step_id`. A cell error is recorded as a failed Step and returns to the
waiting state so the Agent can append a corrected follow-up cell; already executed cells are never
rewritten. Call `execution_finish` with the current version when no more cells are needed. If the
retained kernel is lost or an infrastructure failure makes its state untrustworthy, the DYNAMIC
execution fails as non-retryable; the Agent must submit a new Execution because automatic replay
of already executed dynamic cells is intentionally not supported.

`EXECUTION_MAX_RUNTIME_SECONDS` defaults to five days and starts when a worker first claims the
Execution. `DYNAMIC_STEP_WAIT_TIMEOUT_SECONDS` defaults to one hour and is reset after every
dynamic cell. The effective wait deadline never exceeds the total execution deadline. A missed
Agent deadline produces `DYNAMIC_WAIT_TIMEOUT`; a missing retained kernel produces `KERNEL_LOST`.
A manually removed Jupyter server produces `JUPYTER_UNAVAILABLE`. These terminal dynamic failures
are non-retryable and their kernels are deleted when still reachable. Temporary health-probe
`OFFLINE` state alone does not immediately fail waiting work; the persisted deadline remains the
guard while the server recovers.

`execution_cancel` also requires an idempotency key. It first records `CANCEL_REQUESTED`; the
worker then interrupts and deletes the kernel before recording `CANCELLED`.

`execution_retry` is accepted only for a `FAILED` execution with a supported `retry_strategy`. A
notebook cell error preserves that kernel for `FAILED_KERNEL_RETENTION_SECONDS` and uses
`FROM_FAILED_STEP`. Worker shutdown, lease expiry, and Jupyter connectivity failure use
`FROM_START` with a new kernel because prior in-memory state cannot be trusted. Attempt history
preserves the failure type, retry strategy, and kernel cleanup result. A retained kernel counts
against server capacity and is deleted automatically when its retry window expires. See
[Execution Recovery](docs/execution-recovery.md) for the state and event contract.

On graceful process shutdown, Worker admission stops before active jobs are touched. `/readyz`
reports `worker_accepting=false`, Redis consumption and PostgreSQL queue reconciliation stop, and
active jobs may finish for up to `EXECUTION_DRAIN_TIMEOUT_SECONDS`. Only jobs still running after
that deadline enter the existing `WORKER_SHUTDOWN` cleanup and recovery path. `/healthz` remains a
process liveness check, while `/workerz` reports `ACCEPTING`, `DRAINING`, or `STOPPED` and the local
active execution count.

All MCP list Tools return `{items, nextCursor}`. `nextCursor` is an opaque continuation token:
clients and agents must pass it back unchanged as the next call's `cursor` while keeping the same
filters. REST list endpoints use the equivalent `{items, next_cursor, has_more}` envelope. Keyset
pagination avoids skipped or duplicated pages caused by offset shifts during long-running work.
These are normal MCP Tool calls with declared input/output schemas; no private transport method is
introduced. The custom Tool result mirrors MCP's opaque cursor naming convention while remaining
valid structured Tool content.

`execution_attempt_list` returns worker Attempts in order, including the selected Jupyter
server, kernel, lease/heartbeat times, outcome, and only the Steps actually run by that Attempt.
Each Step history row snapshots its skill, tool, inputs, outputs, error, and timestamps, so a retry
does not overwrite evidence from the earlier failure. `execution_event_list` returns the
transactional Outbox timeline and current Redis publication state. `execution_trace_get` combines
the current Execution, Attempt/Step histories, and events for an end-to-end frontend detail view.
Secret-shaped keys in historical inputs, outputs, and event payloads are defensively redacted.

Execution-scoped files created or modified under type directories in `artifacts/` are detected
after each Step. The standard directories are `datasets`, `plots`, `models`, `metrics`, `reports`,
`logs`, and `other`; their directory type takes precedence over the filename extension. Successful
files are `AVAILABLE`; files left by a failed cell are `INCOMPLETE`, so a later retry produces a
separate Attempt-linked Artifact rather than overwriting the failure evidence. The final `.ipynb`
is registered after successful execution. PV size and SHA-256 are computed by Executor.

Tools can append JSON Lines to `artifacts/manifest.jsonl` to register user-level processed data or
S3 objects outside the execution workspace. Manifest use is optional and does not require every
analysis Tool to accept an Asset ID. S3 metadata and checksum are caller-declared because Executor
does not read the object. See [Artifact Manifest](docs/artifact-manifest.md) for the contract.

`execution_artifact_list` and `execution_artifact_get` expose execution/Attempt/Step references,
storage URI, media type, size, checksum, status, metadata, and a direct parent Artifact or external
Agent Asset ID. `execution_trace_get` includes the same Artifact collection. Registration emits
`execution.artifact_registered` through the Transactional Outbox.

Agent code files and `.ipynb` files are not public execution inputs. Executor owns Notebook
materialization and writes `code/execution-spec.json` plus `notebooks/execution.ipynb` inside the
Execution workspace.

## Jupyter fleet management

`jupyter_server_upsert` accepts a stable name, HTTP endpoint, pool, optional capacity, and token.
A token is required when creating a server and optional when updating one. The token is encrypted
before it is persisted and is never returned by any Tool. Registration immediately probes
`/api/status` and `/api/kernelspecs`; only an enabled `ACTIVE` server is eligible for scheduling.

The background health monitor repeats the probe at
`JUPYTER_HEALTH_POLL_INTERVAL_SECONDS`. A failed server becomes `OFFLINE`, while
`jupyter_server_remove` performs a durable soft disable so historical execution foreign keys
remain valid. `jupyter_server_list` reports the configured limit, active execution count, observed
kernel count, supported kernels, and latest health result. The scheduler selects within the
requested `INTERACTIVE` or `BATCH` pool and skips full, disabled, unhealthy, or incompatible
servers.

`INTERACTIVE` and `BATCH` are strict scheduling partitions. A BATCH Execution is never assigned to
an INTERACTIVE server, even if that server has free capacity, and DRAINING/OFFLINE servers are not
fallback targets. When all eligible BATCH servers are full, the Execution remains `QUEUED` and
PostgreSQL reconciliation retries assignment after capacity becomes available. The local
`batch-jupyter` profile provides two one-capacity servers for this behavior; production capacity is
configured per manually registered server.

Use `jupyter_server_set_state` with `DRAINING` before server maintenance. Existing executions and
retained retry kernels remain attached, while new work is excluded from that server. The response
sets `drain_complete=true` after its active/reserved count reaches zero. `ACTIVE` probes the server
before allowing new work again; `remove` is the separate operation for durable disablement.

`jupyter_server_list` exposes server status, configured capacity, active execution count, and
observed kernel count. Capacity usage includes running/waiting Attempts and retained retry kernels,
including work draining from a server; it can temporarily exceed currently schedulable capacity
during maintenance. See
[Jupyter Pool Operations](docs/jupyter-pools.md) for registration, scale-up, drain, and local E2E
procedures.

## Shared PV contract

The local bind mount is `./notebook_dir:/workspace/pv`. Kubernetes should mount the shared PVC at
the same in-container root. Execution files use the following stable hierarchy:

```text
/workspace/pv/users/{user_id}/projects/{project_id}/sessions/{session_id}/executions/{execution_id}/
    ├── code/
    ├── notebooks/execution.ipynb
    ├── artifacts/
    │   ├── datasets/
    │   ├── plots/
    │   ├── models/
    │   ├── metrics/
    │   ├── reports/
    │   ├── logs/
    │   └── other/
    └── checkpoints/
```

Raw data remains in S3. PATH submissions are resolved under the configured PV root and path
traversal is rejected. The reusable processed-data hierarchy is intentionally not fixed until
[Deferred Decisions](docs/deferred-decisions.md) DD-002 is resolved.

## Consistency and delivery

Submission and its `execution.submitted` OutboxEvent are committed in one PostgreSQL transaction.
Cancellation and `execution.cancel_requested` work the same way. A background publisher claims
pending rows with `FOR UPDATE SKIP LOCKED`, adds each event to the configured Redis Stream, and
then marks it published. A crash between Redis `XADD` and the database update can create a
duplicate, so consumers must deduplicate on `event_id`.

The consumer group treats Redis as a wake-up channel and reconciles `QUEUED` and
`CANCEL_REQUESTED` rows from PostgreSQL, so an acknowledged or lost notification does not lose the
execution. A message left Pending by a dead consumer is reclaimed with `XAUTOCLAIM` after
`EXECUTION_PENDING_CLAIM_IDLE_MILLISECONDS`; the new Worker handles and acknowledges it using the
same PostgreSQL state guards. Malformed messages and unsupported aggregate/event families are
acknowledged only after sanitized metadata is written to `REDIS_DEAD_LETTER_STREAM`. Valid
non-command `execution.*` notifications are intentionally acknowledged without dispatch because
the Agent consumer group still needs those events. See [Event Delivery](docs/event-delivery.md)
for the ACK, reclaim, and DLQ contract.

Active attempts renew a PostgreSQL lease. A dynamic Attempt in
`WAITING_FOR_NEXT_STEP` releases its worker lease but keeps its kernel reservation, so it counts
against that Jupyter server's capacity. A background audit verifies retained kernels and enforces
both stored deadlines after Executor restarts. An expired active lease is failed safely and can be
retried by a later retry workflow; automatic re-execution is intentionally not enabled yet.

Redis Stream trimming is deliberately disabled. The Stream is shared with Agent-owned consumer
groups, so a retention policy must account for every group's delivered and Pending positions before
entries can be removed safely. PostgreSQL Outbox rows are also retained because they back the
frontend execution event timeline.

No database migration runs automatically during service startup. Deployments must run Alembic as
a release or init job before readiness can pass.

## Configuration and secrets

All settings use environment variables; `.env` is ignored by Git. `DATABASE_URL` and `REDIS_URL`
are represented as secret settings and are never intentionally logged. `.env.example` contains
local-only credentials. Inject production values through the Kubernetes secret mechanism.

`MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` are fail-closed allowlists for the SDK's DNS-rebinding
protection. Add the Kubernetes gateway hostname and origin before deployment; do not disable the
protection to make a proxy work.

`REDIS_DEAD_LETTER_STREAM` must differ from `REDIS_STREAM`. Pending recovery cadence, minimum idle
time, and batch size are configured with `EXECUTION_PENDING_CLAIM_INTERVAL_SECONDS`,
`EXECUTION_PENDING_CLAIM_IDLE_MILLISECONDS`, and `EXECUTION_PENDING_CLAIM_BATCH_SIZE`. The minimum
idle time should exceed normal message dispatch latency; it does not need to match the much longer
Execution lease because Redis only wakes the PostgreSQL-backed Worker.

Every Executor process must share `EXECUTION_CONSUMER_GROUP` and use a unique
`EXECUTION_CONSUMER_NAME`; inject the Kubernetes Pod name in production. See
[Multi-Executor Operations](docs/multi-executor-operations.md) for locking invariants, lease
recovery, deployment behavior, and the real crash E2E.

Set Kubernetes `terminationGracePeriodSeconds` greater than
`EXECUTION_DRAIN_TIMEOUT_SECONDS + EXECUTION_SHUTDOWN_CLEANUP_SECONDS` plus a shutdown buffer.
Executions longer than the configured drain window cannot move their live Jupyter WebSocket to
another Pod; they use the documented failure and explicit retry path when the deadline expires.

The bootstrap Jupyter token is supplied through `JUPYTER_TOKEN`. It is loaded by the mounted
Jupyter config and sent by the Executor only in the `Authorization` header. Dynamically registered
tokens are encrypted with `JUPYTER_CREDENTIAL_KEY` before PostgreSQL storage. Neither plaintext
token is placed in request URLs or responses. Rotate the encryption key only with a credential
re-encryption procedure; replacing it directly makes existing dynamic credentials unreadable.

## Package structure

```text
src/executor_service/
├── domain/           # entities, state rules, ports
├── application/      # submit/get/cancel use cases
├── infrastructure/   # SQLAlchemy, Redis Outbox, Jupyter gateway and fleet worker
└── interfaces/       # MCP SDK schemas/tools and HTTP host
```
