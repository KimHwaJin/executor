# Executor Service

Asynchronous Runtime execution control plane exposed through MCP 2026-07-28 Streamable HTTP and a
versioned REST API. PostgreSQL is the source of truth. MCP Tool and REST calls persist work and
return immediately while a Redis consumer worker executes SINGLE work or incrementally submitted
MULTI Operations through a Runtime Driver. Jupyter REST/WebSocket is the first implemented driver.

## Implemented scope

- Official MCP Python SDK 2.x `MCPServer`, exposed at `POST /mcp`
- REST execution facade, OpenAPI, Swagger UI, and ReDoc under `/api/v1`, `/openapi.json`, `/docs`,
  and `/redoc`
- Execution tools: `execution_submit`, `execution_get`,
  `execution_cancel`, `execution_retry`, `execution_operation_create`, `execution_finalize`,
  `execution_list`, `execution_step_list`, `execution_attempt_list`,
  `execution_attempt_get`, `execution_attempt_step_list`,
  `execution_notebook_read`, `execution_notebook_cell_read`,
  `execution_event_list`,
  `execution_artifact_list`, `execution_artifact_get`
- Runtime Target tools: `runtime_target_upsert`, `runtime_target_list`,
  `runtime_target_get`, `runtime_target_probe`, `runtime_target_disable`,
  `runtime_target_set_state`
- Execution, ExecutionStep, and OutboxEvent persistence with SQLAlchemy 2 and Alembic
- Transactional Outbox publisher with at-least-once Redis Stream delivery
- Redis consumer group worker with PostgreSQL reconciliation, stale Pending recovery, and DLQ
- Multi-Executor coordination through PostgreSQL row locks, unique lease owners, and crash recovery
- Jupyter REST/WebSocket kernel execution, interrupt, and deletion
- Multi-target Runtime registry, encrypted credentials, health probes, capacity scheduling,
  execution attempts, leases, and heartbeats
- Strict INTERACTIVE/BATCH Runtime scheduling isolation with a two-target local BATCH topology
- Safe target draining and retained-session retry from a failed Step
- Classified Tool/infrastructure failures, graceful Worker shutdown, and FROM_START recovery
- Append-only MULTI Operations with optimistic version checks and same-session continuation
- Operation wait/total runtime deadlines, retained-session audits, and orphan cleanup
- Immutable per-Attempt Step history and an end-to-end execution event trace
- Automatic and Manifest-based Artifact registration with checksum and lineage
- Runtime-owned `.ipynb` output and execution-scoped artifacts on Jupyter shared storage
- Fenced immutable source and Step-result files on the Agent/Executor shared volume; PostgreSQL
  retains authoritative state, bounded summaries, and canonical relative references
- W3C trace-context propagation across HTTP/MCP, PostgreSQL Outbox, Redis Streams, Worker,
  and Jupyter operations with optional OTLP export to Arize Phoenix
- `/healthz` and `/readyz` operational endpoints
- PostgreSQL, Redis, custom Python slim INTERACTIVE/BATCH Jupyter fleets, and opt-in Phoenix
  through Docker Compose
- Authenticated Jupyter resource endpoint with cgroup v2 CPU and memory measurement

MCP Tasks are deliberately not used. `execution_submit` returns an `execution_id` while the
execution starts as `QUEUED`. Poll with `execution_get` or request cancellation with
`execution_cancel`. MULTI execution accepts one or more initial Steps as an Operation, returns
`WAITING_FOR_OPERATION` after the Operation succeeds or fails, and accepts another append-only
Operation through `execution_operation_create`. `execution_finalize` persists the final notebook and deletes the
retained Runtime session. MCP Tasks are not required for this lifecycle.

## External test harnesses

Non-Executor systems used for local integration tests live under
[`test_harness/`](test_harness/README.md). The Jupyter harness owns its image, kernels, server
extension, native runner, and workspace; the Agent harness is an independent LangGraph/LangChain
project. Its E2E scenario checkpoints after MCP submission, resumes the same LangGraph thread from
an Agent-owned Redis consumer group, and verifies Executor state plus Runtime-owned Jupyter output.
Their dependencies and generated data do not enter the Executor service package.

Quality gates are documented in [docs/quality-gates.md](docs/quality-gates.md).
Run the platform-independent static and unit gate with:

```bash
uv run python scripts/quality_gate.py
```

## Deferred decisions

Return-value materialization, reusable Asset promotion, and user-versus-project Asset visibility
are intentionally not implemented yet. Their agreed constraints, open questions, and resume
criteria are tracked in [Deferred Decisions](docs/deferred-decisions.md). Update that decision log
before implementing or changing any deferred behavior.

Cross-service architecture proposals and accepted decisions are tracked separately in
[Architecture Decisions](docs/architecture-decisions.md). A `PROPOSED` ADR is a discussion baseline,
not authorization to implement its undecided details.

Confirmed hardening work that must be completed before production is tracked in
[Production Readiness](docs/production-readiness.md). These items are implementation requirements,
not deferred product decisions.

## Local setup

The only universal prerequisite is `uv`, which installs the pinned CPython 3.12 runtime when it is
not already present. Docker and Docker Compose are required only for the Compose workflow below.
They are optional when PostgreSQL and Redis already run locally. The cross-platform native
Jupyter bootstrap supports Linux, macOS, and Windows PowerShell without WSL; see
[`test_harness/jupyter/README.md`](test_harness/jupyter/README.md#native-installation-without-docker). The
Executor and test Agent remain normal host processes in that topology.

```bash
cp .env.example .env
uv sync --dev
docker compose up -d postgres redis jupyter
uv run alembic upgrade head
uv run executor-service
```

To build and run the Executor application together with PostgreSQL, Redis, and Jupyter, use the
full Compose stack instead. The one-shot `migrate` service upgrades the schema before `executor`
starts. Agent and Executor share `./shared_dir`; the Jupyter fleet separately mounts
`./test_harness/jupyter/workspace` at `/workspace/pv`.

```bash
cp .env.example .env
docker compose up -d --build --wait
docker compose ps -a
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/readyz
```

`migrate` should show `Exited (0)`, while `executor`, `postgres`, `redis`, and `jupyter` should be
running and healthy. When invoking the host-side single-server smoke test against this stack,
register the Jupyter endpoint as its Compose-internal address so the Executor container can reach
it:

```bash
SINGLE_JUPYTER_ENDPOINT=http://jupyter:8888 \
  uv run python scripts/single_jupyter_smoke.py
```

Stop the stack with `docker compose down`. Named PostgreSQL and Redis volumes, and the bind-mounted
`shared_dir` and Jupyter-owned `test_harness/jupyter/workspace`, are retained unless explicitly
removed.

For MCP calls from another machine, append the Executor host or IP (including `:*` when any port
is acceptable) to `MCP_ALLOWED_HOSTS_DOCKER` and its browser origin to
`MCP_ALLOWED_ORIGINS_DOCKER` before recreating `executor`. Keep these allowlists narrow; the
Streamable HTTP transport rejects unlisted Host headers by design.

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
uv run python scripts/jupyter_timeout_abort_smoke.py
uv run python scripts/jupyter_execution_smoke.py
uv run python scripts/path_execution_spec_smoke.py
uv run python scripts/single_jupyter_smoke.py
uv run python scripts/single_execution_observability_smoke.py
uv run python scripts/jupyter_multi_smoke.py
uv run python scripts/jupyter_multi_lifecycle_smoke.py
uv run python scripts/jupyter_cancel_smoke.py
uv run python scripts/jupyter_failure_smoke.py
uv run python scripts/jupyter_fleet_smoke.py
uv run python scripts/jupyter_shared_storage_failover_smoke.py
uv run python scripts/jupyter_retry_smoke.py
uv run python scripts/jupyter_retry_offline_recovery_smoke.py
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

For repeatable local load and long-running validation, start the four-server fleet with explicit
test resource limits, run the preflight, and use the quick suite:

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml \
  --profile multi-jupyter --profile batch-jupyter up -d --build --wait
uv run python scripts/local_test_preflight.py
uv run python scripts/local_validation_suite.py
```

The quick suite covers consolidated mixed text/table/JSON/image/Artifact results and a configurable
Jupyter soak. `--full` adds lifecycle and 30-execution load tests; `--include-faults` opts into
disruptive process and dependency failure scenarios. Detailed commands, safety boundaries, and
report locations are documented in
[Executor Resilience Testing](docs/executor-resilience-testing.md).

The authoritative output architecture is documented in
[Shared execution result storage](docs/shared-result-storage.md). Jupyter IOPub output is streamed
directly into a fenced partial directory on the Agent/Executor shared volume and atomically sealed
before PostgreSQL and Redis publish its canonical reference. PostgreSQL and Redis never retain
complete text, image, or binary bodies.

The scripts that start their own Executor processes require the Compose `executor` service to be
stopped first; unique Redis Stream names do not isolate PostgreSQL queue reconciliation. Those
scripts fail fast with the required command instead of allowing another Worker to claim their rows.

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
uv run alembic upgrade head
uv run alembic current
uv run alembic check
```

Revision `0001` is the complete 2026-08-19 pre-release schema baseline. Revision `0002` is the
current head and adds internal Worker lease fencing. Databases on the supported baseline upgrade
normally with `alembic upgrade head`; databases on a discarded pre-baseline development revision
must be recreated. See [Database Operations](docs/database-operations.md).

## Tool contracts

The same execution lifecycle is available as REST without internally calling MCP. REST requests
and responses have transport-specific DTOs but map to the same application commands, PostgreSQL
transactions, Outbox, and Worker. See [Executor REST API v1](docs/rest-api.md) for every endpoint
and request examples.

`execution_submit` accepts one `request` object. Important fields are:

- `idempotency_key`: required for safe retries; reuse with different content is rejected
- `lifecycle.operation_mode`: `SINGLE` or `MULTI`
- `trigger.type`: `INTERACTIVE` or `BATCH`; `trigger.actor` is the audit principal
- `runtime.type`: Runtime Driver kind; currently `JUPYTER`
- `runtime.profile`: one of the target's supported profiles; for Jupyter this is a kernelspec name
- `operation.spec`: ExecutionSpec `1.0`; every Step independently supplies INLINE code or a
  `.py` input-storage PATH plus SHA-256
- `operation.operation_timeout_seconds`: optional whole-Operation limit
- `context`: Agent-owned user/project/session/Task IDs; Executor creates `execution_id`

Every public mutation records `created_by`/`updated_by` attribution on the affected Execution,
Step, Attempt, Artifact, Outbox Event, or Runtime Target where applicable. `context.user_id` owns
the Execution and its results. Interactive submits accept `AGENT` or `USER`; a USER actor ID must
match `context.user_id`. Batch submits require a `BATCH` actor whose ID identifies the schedule or
manual batch trigger and may differ from the owning user.

`runtime_pool` is not accepted from callers. Executor derives `INTERACTIVE` or `BATCH` from
`trigger.type`, then selects a healthy compatible Runtime Target with available capacity inside
that pool.
`context.workflow_id` is optional for both triggers.

ExecutionSpec stays at schema version `1.0` during pre-release development. Each ordered Step uses
`PYTHON_EXECUTE` and independently embeds INLINE code or references one UTF-8 `.py` file below
`INPUT_HOST_ROOT` with a relative path and SHA-256. Executor persists the resolved source and
provenance on that ExecutionStep. The Jupyter Driver executes each Step as one code cell. See
[ExecutionSpec v1](docs/execution-spec.md).

For `MULTI`, submit one or more consecutive ExecutionSpec Steps starting at sequence `0`. After the execution reaches
`WAITING_FOR_OPERATION`, call `execution_operation_create` with the current `version`, a new idempotency
key, and an ExecutionSpec containing one or more next consecutive Steps. A stale version, gap, or
duplicate sequence is rejected. Each Operation records metadata and its Executor-owned IDs; each
Step records its own resolved source provenance. A Runtime error is recorded as a failed Step and returns to the
waiting state so the Agent can append a corrected follow-up Operation; already executed Steps are
never rewritten. Call `execution_finalize` with the current version when no more Steps are needed. If the
retained Runtime session is lost or an infrastructure failure makes its state untrustworthy, the MULTI
execution fails as non-retryable; the Agent must submit a new Execution because automatic replay
of already executed Steps is intentionally not supported.

## Storage ownership

- The Agent writes PATH-type Step `.py` files into Agent/Executor input storage. Executor reads
  them through `INPUT_HOST_ROOT`; Jupyter does not need this volume.
- Jupyter creates execution workspaces, notebooks, artifacts, datasets, and manifests on its own
  shared storage. All Jupyter Runtime Targets share that storage.
- Mounting the same shared PVC on every Jupyter Runtime Target is an operator-owned deployment
  contract; Executor does not discover or manage PV/PVC identity.
- Executor never opens Jupyter files locally. PostgreSQL stores Runtime-relative paths and
  Jupyter-computed metadata/checksums; notebook content is read through an available Jupyter target.
- Runtime retry prefers the original target/kernel. Storage-only reads prefer that target but may
  fall back to another healthy target attached to the same shared storage.

`EXECUTION_MAX_RUNTIME_SECONDS` defaults to five days and starts when a worker first claims the
Execution. Each MULTI request supplies `lifecycle.operation_wait_timeout_seconds`; its deadline is
reset after every Operation and never exceeds the total Execution deadline. A missed Agent deadline
produces `OPERATION_WAIT_TIMEOUT`; a missing retained session produces `RUNTIME_SESSION_LOST`.
A disabled Runtime Target produces `RUNTIME_UNAVAILABLE`. These terminal MULTI failures are
non-retryable and their sessions are deleted when still reachable. Temporary health-probe
`OFFLINE` state alone does not immediately fail waiting work; the persisted deadline remains the
guard while the server recovers.

`execution_cancel` also requires an idempotency key. It first records `CANCEL_REQUESTED`; the
worker then interrupts and deletes the Runtime session before recording `CANCELLED`.

`execution_retry` is accepted only for a `FAILED` SINGLE execution with a supported
`retry_strategy`. Its `operation.operation_id` is the same ID accepted at submit time and it creates a new
Attempt; it does not create a new Operation. A
notebook cell error preserves that session for `FAILED_SESSION_RETENTION_SECONDS` and uses
`FROM_FAILED_STEP`. Worker shutdown, lease expiry, and Runtime connectivity failure use
`FROM_START` with a new session because prior in-memory state cannot be trusted. Attempt history
preserves the failure type, retry strategy, and session cleanup result. A retained session counts
against target capacity and is deleted automatically when its retry window expires. See
[Execution Recovery](docs/execution-recovery.md) for the state and event contract.

On graceful process shutdown, Worker admission stops before active jobs are touched. `/readyz`
reports `worker_accepting=false`, Redis consumption and PostgreSQL queue reconciliation stop, and
active jobs may finish for up to `EXECUTION_DRAIN_TIMEOUT_SECONDS`. Only jobs still running after
that deadline enter the existing `WORKER_SHUTDOWN` cleanup and recovery path. `/healthz` remains a
process liveness check, while `/workerz` reports `ACCEPTING`, `DRAINING`, or `STOPPED` and the local
active execution count.

All MCP and REST list operations return `{items, next_cursor, has_more}`. `next_cursor` is an
opaque continuation token:
clients and agents must pass it back unchanged as the next call's `cursor` while keeping the same
filters. Keyset
pagination avoids skipped or duplicated pages caused by offset shifts during long-running work.
These are normal MCP Tool calls with declared input/output schemas; no private transport method is
introduced. MCP protocol-native list operations remain owned by the official SDK; these are
Executor-defined Tool result contracts shared with REST.

`execution_attempt_list` returns lightweight worker Attempt history with outcome and Step count.
Use `execution_attempt_get` for the selected Runtime Target, session, immutable Runtime
type/profile snapshot, lease/heartbeat times, and recovery details. Use
`execution_attempt_step_list` for the Steps actually run by that Attempt. Each Step history row
snapshots its skill, tool, inputs, outputs, error, and timestamps, so a retry
does not overwrite evidence from the earlier failure. `execution_event_list` returns the
transactional Outbox timeline and current Redis publication state. Frontends compose the current
Execution with Attempt/Step, event, and Artifact list endpoints for an end-to-end detail view.
Secret-shaped keys in historical inputs, outputs, and event payloads are defensively redacted.

Execution-scoped files created or modified under type directories in `artifacts/` are detected
after each Step. The standard directories are `datasets`, `plots`, `models`, `metrics`, `reports`,
`logs`, and `other`; their directory type takes precedence over the filename extension. Successful
files are `AVAILABLE`; files left by a failed cell are `INCOMPLETE`, so a later retry produces a
separate Attempt-linked Artifact rather than overwriting the failure evidence. The final `.ipynb`
is registered after successful execution. PV size and SHA-256 are computed inside Jupyter storage
and only the verified metadata is returned to Executor.

Tools can append JSON Lines to `artifacts/manifest.jsonl` to register user-level processed data or
S3 objects outside the execution workspace. Manifest use is optional and does not require every
analysis Tool to accept an Asset ID. S3 metadata and checksum are caller-declared because Executor
does not read the object. See [Artifact Manifest](docs/artifact-manifest.md) for the contract.

`execution_artifact_list` and `execution_artifact_get` expose execution/Attempt/Step references,
storage URI, media type, size, checksum, status, metadata, and a direct parent Artifact or external
Agent Asset ID. Registration emits
`execution.artifact_registered` through the Transactional Outbox.

Agent-authored `.ipynb` files are not execution inputs. Agent-authored Python is supplied per Step,
either INLINE or as a `.py` PATH. Executor builds the Notebook document from executed Steps and
outputs, while Jupyter writes `notebooks/execution.ipynb` into its own shared storage. Agent-authored
reports can be materialized through `execution_artifact_create` or
`POST /api/v1/executions/{execution_id}/artifacts`; REPORT content is written below `reports/` and
may also be appended as a Markdown notebook cell.

## Runtime fleet management

`runtime_target_upsert` accepts a stable name, `runtime_type`, driver-owned `connection_config`,
pool, optional capacity, and credential. For the Jupyter driver, `connection_config` contains only
an HTTP(S) `endpoint`, while the credential is its token. A credential is required when creating a
target and optional when updating one. It is encrypted before persistence and never returned by
MCP or REST. Registration immediately probes the driver; only an enabled `ACTIVE` target is
eligible for scheduling. A target's `runtime_type` is immutable after creation; register a new
target name when introducing a different Runtime Driver.

The background health monitor repeats the probe at
`RUNTIME_HEALTH_POLL_INTERVAL_SECONDS`. A failed health or kernel-profile probe makes the target
`OFFLINE`; a resource-only failure leaves it `ACTIVE` with stale resource data. Fresh targets are
ranked by slot, CPU, and memory pressure, with the memory admission threshold configured by
`RUNTIME_MEMORY_ADMISSION_LIMIT`. If all resource observations exceed
`RUNTIME_RESOURCE_MAX_AGE_SECONDS`, scheduling falls back to the least effective admission-usage
ratio. Meanwhile,
`runtime_target_disable` performs a durable disable so historical execution foreign keys
remain valid. `runtime_target_list` reports capacity, active executions, observed sessions,
supported profiles, and the latest health result. The scheduler selects within the requested
`INTERACTIVE` or `BATCH` pool and skips full, disabled, unhealthy, or incompatible targets.
Only profiles listed in `RUNTIME_ALLOWED_PROFILES` are advertised and schedulable, even if a
Runtime Driver reports additional environments.

Capacity admission uses `max(active_execution_count, active_session_count)` while the persisted
session observation is fresh. The DB count includes running/waiting Attempts, retained retries,
and cleanup `PENDING`/`FAILED` sessions. Failed probes retain the last observed count but mark it
stale; stale observations are not treated as zero and scheduling falls back to DB reservations.
Runtime Target responses expose the two counts, their effective `admission_used_count`, freshness,
availability, and capacity-blocked state.

`INTERACTIVE` and `BATCH` are strict scheduling partitions. A BATCH Execution is never assigned to
an INTERACTIVE target, even if it has free capacity, and DRAINING/OFFLINE targets are not fallback
targets. When all eligible BATCH targets are full, the Execution remains `QUEUED` and
PostgreSQL reconciliation retries assignment after capacity becomes available. The local
`batch-jupyter` profile provides two one-capacity servers for this behavior; production capacity is
configured per manually registered target.

Use `runtime_target_set_state` with `DRAINING` before target maintenance. Existing executions and
retained retry sessions remain attached, while new work is excluded from that target. The response
sets `drain_complete=true` after its active/reserved count reaches zero. `ACTIVE` probes the target
before allowing new work again; `disable` is the separate durable disablement operation.

`runtime_target_list` exposes target status, configured capacity, active execution count, and
observed session count. Capacity usage includes running/waiting Attempts and retained retry sessions,
including work draining from a target; it can temporarily exceed currently schedulable capacity
during maintenance. See
[Runtime Target Operations](docs/runtime-targets.md) for registration, scale-up, drain, and local E2E
procedures.

The same fleet registry is available to operators through REST at `/api/v1/runtime-targets` and
`/api/v1/runtime-pools`. REST supports registration, filtered cursor listing, detail, immediate
probe, drain, activate, disable, and a deliberately restricted hard purge. Hard purge requires
the exact target name, an already disabled `OFFLINE` target, and no Execution or Attempt reference;
a successful purge preserves an immutable audit tombstone and never cascades into execution
history. Credentials are accepted only on upsert and credentials are absent from every response.
The non-secret endpoint is returned as `runtime.connection_config.endpoint`.

## Jupyter shared storage contract

Every Jupyter target mounts the local `./test_harness/jupyter/workspace` or production shared PVC
at its configured contents root. Executor does not mount it. Execution files use this stable
Jupyter-relative hierarchy:

```text
/workspace/pv/users/{user_id}/projects/{project_id}/sessions/{session_id}/executions/{execution_id}/
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

Raw data remains in S3. PATH submissions are resolved separately under Executor's
`INPUT_HOST_ROOT`, and path traversal is rejected. The reusable processed-data hierarchy is intentionally not fixed until
[Deferred Decisions](docs/deferred-decisions.md) DD-002 is resolved.

## Consistency and delivery

Submission commits both its Agent-facing `execution.submitted` event and internal
`operation.ready` message in one PostgreSQL transaction. Cancellation uses the same pattern. A
background publisher claims pending rows with `FOR UPDATE SKIP LOCKED`, routes each row to either
`executor.events` or `executor.work`, and
then marks it published. A crash between Redis `XADD` and the database update can create a
duplicate, so consumers must deduplicate on `event_id`.

The consumer group treats Redis as a wake-up channel and reconciles `QUEUED` and
`CANCEL_REQUESTED` rows from PostgreSQL, so an acknowledged or lost work message does not lose the
execution. A message left Pending by a dead consumer is reclaimed with `XAUTOCLAIM` after
`EXECUTION_PENDING_CLAIM_IDLE_MILLISECONDS`; the new Worker handles and acknowledges it using the
same PostgreSQL state guards. Malformed internal messages are acknowledged only after sanitized
metadata is written to `REDIS_WORK_DEAD_LETTER_STREAM`. Executor Workers never consume Agent
integration events. See [Event Delivery](docs/event-delivery.md)
for the ACK, reclaim, and DLQ contract.

Active attempts renew a PostgreSQL lease. A MULTI Attempt in
`WAITING_FOR_OPERATION` releases its worker lease but keeps its session reservation, so it counts
against that Runtime Target's capacity. A background audit verifies retained sessions and enforces
both stored deadlines after Executor restarts. An expired active lease is failed safely and can be
retried by a later retry workflow; automatic re-execution is intentionally not enabled yet.

Redis Stream trimming is deliberately disabled. `executor.work` and `executor.events` have
independent consumers, and each retention policy must account for its group's delivered and Pending
positions. PostgreSQL Outbox rows are also retained because they back the
frontend execution event timeline.

Every published Stream entry and JSON payload uses the versioned Executor event contract. Agent
consumers must use their own consumer group and durably deduplicate on `event_id` before ACK. See
[Execution Event Contract v2](docs/execution-events-v2.md) and the reference
`scripts/agent_event_consumer_example.py`.

The real-Jupyter regression suite includes a combined SINGLE failure/retry/cancellation scenario
that reconciles REST/MCP history with PostgreSQL, Outbox, Redis, PV Artifacts, and Runtime sessions.
See [Executor Resilience Testing](docs/executor-resilience-testing.md).

The same suite covers MULTI same-session continuation, append-only failure correction, explicit
finalization, and running cancellation through `scripts/multi_execution_lifecycle_e2e.py`.

No database migration runs automatically during service startup. Deployments must run Alembic as
a release or init job before readiness can pass.

The application uses a bounded SQLAlchemy PostgreSQL connection pool. Pool capacity, checkout
timeout, connection recycling, per-Pod connection budgeting, and critical query-plan verification
are documented in [Database Operations](docs/database-operations.md).

## Configuration and secrets

All settings use environment variables; `.env` is ignored by Git. `DATABASE_URL` and `REDIS_URL`
are represented as secret settings and are never intentionally logged. `.env.example` contains
local-only credentials. Inject production values through the Kubernetes secret mechanism.

`MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` are fail-closed allowlists for the SDK's DNS-rebinding
protection. Add the Kubernetes gateway hostname and origin before deployment; do not disable the
protection to make a proxy work.

`REDIS_WORK_STREAM`, `REDIS_EVENT_STREAM`, `REDIS_WORK_DEAD_LETTER_STREAM`, and
`REDIS_EVENT_DEAD_LETTER_STREAM` must all differ. Pending recovery cadence, minimum idle
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

Executor starts with an empty Runtime Fleet. Every Jupyter endpoint and credential is registered
through the Runtime Target REST or MCP API and the credential is encrypted with
`RUNTIME_CREDENTIAL_KEY` before PostgreSQL storage. Plaintext credentials are not placed in request
URLs or responses. Rotate the encryption key only with a credential re-encryption procedure;
replacing it directly makes existing dynamic credentials unreadable.

## Kubernetes deployment

A Kubernetes Deployment, ClusterIP Service, release migration Job, environment ConfigMap, and
Secret key example are provided in [deploy/kubernetes](deploy/kubernetes/README.md). The baseline
mounts only the Agent/Executor input PVC; Jupyter notebook and artifact storage remains attached to
the Jupyter fleet and is accessed through Jupyter APIs.

## Package structure

```text
src/executor_service/
├── domain/           # entities, state rules, ports
├── application/      # submit/get/cancel use cases
├── infrastructure/   # SQLAlchemy, Redis Outbox, Runtime Drivers and fleet worker
└── interfaces/       # MCP SDK schemas/tools and HTTP host
```
