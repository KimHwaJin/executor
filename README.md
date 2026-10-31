# Executor Service

Artifact filename and path policy: [규칙 및 배포 안내](docs/artifact-filename-policy.md).

Asynchronous Runtime execution control plane exposed through MCP 2026-07-28 Streamable HTTP and a
versioned REST API. PostgreSQL is the source of truth. MCP Tool and REST calls persist work and
return immediately while Workers, polling PostgreSQL, execute SINGLE work or incrementally
submitted MULTI Operations through a Runtime Driver. Jupyter REST/WebSocket is the first implemented driver.

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
- Execution, ExecutionStep, durable ExecutionEvent, and Outbox persistence with SQLAlchemy 2 and Alembic
- Transactional Outbox publisher with at-least-once Redis Stream delivery
- PostgreSQL-issued per-Execution event ordering with bounded REST/MCP gap recovery
- PostgreSQL-polling Workers: each takes at most `EXECUTION_WORKER_MAX_ACTIVE` Executions, and
  contended rows are skipped rather than waited on
- Multi-Executor coordination through PostgreSQL row locks, unique lease owners, and crash recovery
- Jupyter REST/WebSocket kernel execution, interrupt, and deletion
- Active execution-loss detection without treating silence/low CPU as failure;
  see [deployment requirements and failure semantics](docs/runtime-execution-loss-detection.md).
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
- `/healthz` and `/readyz` operational endpoints
- PostgreSQL-backed Executor-wide drain, activate, and shutdown-readiness status APIs
- PostgreSQL, Redis, and custom Python slim INTERACTIVE/BATCH Jupyter fleets through Docker
  Compose
- Authenticated Jupyter resource endpoint with cgroup v2 CPU and memory measurement

MCP Tasks are deliberately not used. `execution_submit` returns an `execution_id` while the
execution starts as `QUEUED`. Poll with `execution_get` or request cancellation with
`execution_cancel`. MULTI execution accepts one or more initial Steps as an Operation, returns
`WAITING_FOR_OPERATION` after the Operation succeeds or fails, and accepts another append-only
Operation through `execution_operation_create`. `execution_finalize` persists the final notebook and deletes the
retained Runtime session. MCP Tasks are not required for this lifecycle.

## Redis compatibility

To check an existing PostgreSQL/Redis deployment with one command, set
`DATABASE_URL` and `REDIS_URL` in the environment or `.env`, then run:

```bash
uv run python scripts/backend_compatibility_check.py
```

This verifies the service schema read-only and exercises the real submit,
Outbox, Streams, cancellation and event delivery code in a disposable schema
and isolated Redis keys. It does not execute Jupyter code or upgrade the service
schema. See the [compatibility check guide](docs/backend-compatibility-check.md)
for permissions, optional JSON output and limitations.

This branch supports Redis server **6.0.8 and newer**, using `redis-py` 5.3.x
(`uv.lock` pins the tested client). Both Redis 6.0.8 and 7.4 use the same bounded
`XRANGE`/`XDEL` retention implementation.
Redis must permit `EVAL` and the Stream commands used inside its Lua scripts.
Executor starts and stays ready without Redis: the event retention pass checks server
version, command availability, and basic scripting access before its first Redis trim, and
skips Redis work until the check passes. `/workerz` reports `redis_reachable`.
Local Docker Compose now defaults to the exact **`redis:6.0.8`** image and
the dedicated `executor-redis-608` volume. The integration quality gate also
expects 6.0.8 by default; set `EXECUTOR_EXPECT_REDIS_VERSION` explicitly when
validating another supported version. `REDIS_URL` does not select a server
version: Docker Compose (or the infrastructure operator) selects the image.

See [Redis 6.0 compatibility](docs/redis-6-compatibility.md) for ACL requirements,
retention limits, test commands, and deployment precautions.

## Logging

Startup calls `configure_logging()` in
[`src/executor_service/logging_setup.py`](src/executor_service/logging_setup.py).
When the internal `hcp` library is installed it delegates to
`hcp.framework.logger.init_logger()`, which owns its own `logger.yml` and policy.
Otherwise the standard `logger.yml` below is applied, so the same code runs both
outside and inside the company network without an `hcp` dependency. Moving the
code in only needs the `hcp` dependency and the internal `logger.yml`.

The following YAML behavior applies when `hcp` is **not** installed.

The repository-root `logger.yml` is a standard Python `logging.dictConfig`
configuration. Executor module logs and Uvicorn server/access logs use its
handlers and formatters. The default writes plain text to stdout, without log
files, so Docker/Kubernetes log collection works without another volume.
Jupyter and the separate test Agent retain their own logging configuration.

- Edit `formatters.standard.format` to change the output format.
- Edit `root.level` or individual `loggers` levels to change verbosity.
- `LOG_CONFIG_FILE` selects the YAML file. Its local default is `logger.yml`
  relative to the process working directory. For launches from another directory
  or on Windows, set an absolute path, e.g. `C:/executor/logger.yml`.
- `LOG_LEVEL`, when non-empty, overrides only `root.level`. Explicit levels on
  individual loggers (such as `httpx: WARNING`) still take precedence. Unset it
  to let YAML control all levels. Existing `.env` files may still contain this
  override.
- The Docker image contains `/app/logger.yml` and sets `LOG_CONFIG_FILE` to that
  path. To customize without rebuilding, mount a ConfigMap-provided file, for
  example `/etc/executor/logger.yml`, and set `LOG_CONFIG_FILE` accordingly.
- Restart the service after changes. Missing or invalid configuration fails
  startup explicitly rather than silently falling back to another log policy.

Keep this file deployment-owned and trusted: dictConfig can instantiate Python
handler/formatter factories. Do not put credentials in it. If adding file
handlers, provision a writable directory for the non-root user separately;
the read-only Kubernetes root filesystem is not suitable for log files.
After `configure_logging()`, startup adds `DatabaseErrorFilter` to configured
handlers without replacing internal handlers, formatters, levels, or filters.
If the internal library creates/replaces handlers later, call
`install_database_error_filters()` from
`executor_service.infrastructure.db.logging` again after that initialization.
Runtime failure redaction and database diagnostics remain unchanged. This
configuration does not add new request-body or code-output logging, nor does it
automatically expose every `extra` field supplied by module loggers.

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

Runtime error evidence, partial result references, diagnostics rows and operator log fields are
described in [Runtime Diagnostics](docs/runtime-diagnostics.md).

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
docker volume create executor_executor-postgres
docker volume create executor_executor-redis-608
docker compose -f docker-compose.infra.yml up -d --wait
docker compose -f docker-compose.runtime.yml up -d --wait
uv run alembic upgrade head
uv run executor-service
```

PostgreSQL and Redis run in the separate `shared-infra` Compose project using
`docker-compose.infra.yml`. The four Jupyter servers run in the independent `executor-runtimes`
project using `docker-compose.runtime.yml`; `docker-compose.yml` runs only Executor.
Local Executor startup applies migrations when `DB_AUTO_MIGRATE=true`.
Shared PostgreSQL uses the PG17 pgvector image so existing databases with the `vector` extension
remain usable. Enable it per new database with `CREATE EXTENSION IF NOT EXISTS vector` when needed.
Agent and Executor share `./shared_dir`; the Jupyter fleet separately mounts
`./test_harness/jupyter/workspace` at `/workspace/pv`.

```bash
cp .env.example .env
docker volume create executor_executor-postgres
docker volume create executor_executor-redis-608
docker compose -f docker-compose.infra.yml up -d --wait
docker compose -f docker-compose.runtime.yml up -d --build --wait
docker compose up -d --build --wait
docker compose -f docker-compose.infra.yml ps
docker compose -f docker-compose.runtime.yml ps
docker compose ps -a
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/readyz
```

Both infrastructure services, Executor, and the four Jupyter servers should be healthy.
Infrastructure and runtimes must be started first because Compose cannot enforce health dependencies
across projects. Executor joins the shared infrastructure network and the `executor-runtimes`
network (`RUNTIME_NETWORK` can override it). When invoking the host-side single-server smoke test against this stack,
register the Jupyter endpoint as its Compose-internal address so the Executor container can reach
it:

```bash
SINGLE_JUPYTER_ENDPOINT=http://jupyter:8888 \
  uv run python scripts/single_jupyter_smoke.py
```

`docker compose down` stops only Executor; PostgreSQL, Redis, and the four runtimes keep running.
Stop infrastructure separately with `docker compose -f docker-compose.infra.yml stop`.
Stop runtimes separately with `docker compose -f docker-compose.runtime.yml stop`.
Infrastructure uses external volumes, so Compose does not delete its data. The default names reuse
the existing `executor_executor-postgres` and `executor_executor-redis-608` volumes. For a fresh
installation, create the volumes as shown above; if `SHARED_POSTGRES_VOLUME` or
`SHARED_REDIS_VOLUME` is overridden, create those names instead. Shared results and the Jupyter
workspace remain bind-mounted.

Other Compose projects can connect to the same infrastructure by attaching their application
service to the external `shared-infra` network (or `SHARED_INFRA_NETWORK` override):

```yaml
services:
  app:
    networks: [default, shared-infra]
networks:
  shared-infra:
    external: true
    name: shared-infra
```

On that network use `postgres:5432` and `redis:6379`. Host processes use `localhost:5432` and
`localhost:6379`. Each application should use its own PostgreSQL database and Redis key/Stream
prefix; database creation is explicit. Executor continues to use database `executor`, Redis DB 0,
and `executor.*` Streams.

For MCP calls from another machine, append the Executor host or IP (including `:*` when any port
is acceptable) to `MCP_ALLOWED_HOSTS_DOCKER` and its browser origin to
`MCP_ALLOWED_ORIGINS_DOCKER` before recreating `executor`. Keep these allowlists narrow; the
Streamable HTTP transport rejects unlisted Host headers by design.

On native Windows, psycopg async connections need a `SelectorEventLoop` instead of the default
`ProactorEventLoop`. The code does not switch loops (deployments run on Linux), so start the
Executor locally with:

```powershell
uv run uvicorn executor_service.main:create --factory --loop asyncio:SelectorEventLoop
```

Startup migration (`DB_AUTO_MIGRATE=true`) reuses that loop. To run migrations by hand:

```powershell
uv run python -c "import asyncio; asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()); from alembic.config import main; main(['upgrade','head'])"
```

The Runtime Compose stack starts four Jupyter servers against the same local PV:

| Service | Host port | Executor Runtime Pool |
| --- | --- | --- |
| `jupyter` | `8888` | `INTERACTIVE` |
| `jupyter-secondary` | `8889` | `INTERACTIVE` |
| `jupyter-batch-primary` | `8890` | `BATCH` |
| `jupyter-batch-secondary` | `8891` | `BATCH` |

```bash
docker compose -f docker-compose.infra.yml up -d --wait
docker compose -f docker-compose.runtime.yml up -d --wait
docker compose up -d --wait
```

Register each server through the Runtime Target REST or MCP API with its listed pool and
Compose-internal endpoint (`http://<service>:8888`). Runtime registrations persist in PostgreSQL;
an empty or reset database requires registration again.

Operational endpoints:

- MCP: `http://127.0.0.1:8000/mcp`
- REST API: `http://127.0.0.1:8000/api/v1`
- Swagger UI: `http://127.0.0.1:8000/docs`
- OpenAPI: `http://127.0.0.1:8000/openapi.json`
- liveness: `http://127.0.0.1:8000/healthz`
- readiness (PostgreSQL schema, Worker admission; Redis not required): `http://127.0.0.1:8000/readyz`
- Worker lifecycle, active execution count, Redis reachability, and startup reconciliation summary:
  `http://127.0.0.1:8000/workerz`

### Operational diagnostics

Executor uses standard Python logging (`logger.yml`) and durable execution diagnostics.
It does not collect or export OpenTelemetry spans. Execution IDs, Step IDs, failure details,
and ordered public events remain available independently of any trace collector.
See [ADR-005](docs/architecture-decisions.md#adr-005-remove-executor-owned-tracing).

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
```

For repeatable local load and long-running validation, start the four-server fleet with explicit
test resource limits, run the preflight, and use the quick suite:

```bash
docker compose -f docker-compose.infra.yml up -d --wait
docker compose -f docker-compose.runtime.yml -f docker-compose.runtime.test.yml \
  up -d --build --wait
docker compose -f docker-compose.yml -f docker-compose.test.yml \
  up -d --build --wait
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

To migrate automatically before the API/Worker starts, set `DB_AUTO_MIGRATE=true`.
The application default is false; the example `.env` and Docker Compose opt in.
With it disabled, run the Alembic commands above before starting the service.
Automatic and CLI migrations share a PostgreSQL transaction lock. A migration failure
stops startup; an up-to-date database is left unchanged. Migrations require existing
database credentials with schema DDL privileges. Do not overlap old application processes
with incompatible schema changes. See [startup migrations](docs/startup-migrations.md).
The Kubernetes sample uses inline Deployment settings, not ConfigMap/Secret/Job resources.

Revision `0001` is the complete 2026-08-31 schema baseline. Revision `0002` adds
execution diagnostic history; `0003` permits non-retryable `COMPLETION_FAILED`
failures when required post-code delivery fails. Neither upgrade deletes data.
Revision `0004` removes only the approved obsolete trace columns and their values;
Execution, Step, event and Outbox business history is preserved.
Databases built from that baseline can run `alembic upgrade head` directly;
Revision `0005` preserves historical Runtime UUIDs when
removing registrations and simplifies purge to an actor-only request; see
[Runtime deletion](dev_docs/post-runtime-target-purge.md).
Revision `0006` adds the Step run and handover tables used by
[Step handover](docs/execution-recovery.md#step-handover-between-workers), and `0007` adds the
Execution tenant. `0008` allows one Runtime Target per normalized endpoint, `0009` drops the
unused `cancellation_heartbeat_at` column, and the current head `0010` reduces the Outbox to public events (no `destination`/`payload`). Stop older
Executor processes before applying either.
See [required-result completion](docs/required-result-completion.md)
for Step versus Operation/Execution success semantics.
Background cleanup/probe failures and sealed partial output references are described in
[Runtime Diagnostics](docs/runtime-diagnostics.md).
Databases from older, discarded development baselines still require separate
recreation after backup, even when their revision string happens to be `0001`. See
[Database Operations](docs/database-operations.md).

Failure observations and secondary output/notebook/cleanup errors are available
through `GET /api/v1/executions/{execution_id}/diagnostics`; see
[Diagnostic API](dev_docs/execution-diagnostics.md).

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

Identity is not part of the body. Every REST and MCP call names its user, project, session, and
tenant through `X-User-Id`, `X-Project-Id`, `X-Session-Id`, and (relay keys only) `X-Tenant-Id`,
authenticated by `X-Api-Key` in stg and prd. A body `context` or `tenant_id` is rejected. See
[Authentication and identity](docs/rest-api.md#authentication-and-identity).

Every public mutation records `created_by`/`updated_by` attribution on the affected Execution,
Step, Attempt, Artifact, Outbox Event, or Runtime Target where applicable. The `X-User-Id` user owns
the Execution and its results. Interactive submits accept `AGENT` or `USER`; a USER actor ID must
match that user. Batch submits accept `USER` or `BATCH`: a USER actor is the user who
registered the batch and must match that user; a BATCH actor ID identifies a system schedule
or trigger and may differ from the owning user.

`runtime_pool` is not accepted from callers. Executor derives `INTERACTIVE` or `BATCH` from
`trigger.type`, then selects a healthy compatible Runtime Target with available capacity inside
that pool.
Responses expose that scope as `context` (`user_id`, `project_id`, `session_id`). Callers
name their own hierarchy (batch, run, task) in the top-level `metadata`, which `execution.completed`
echoes back.

Every Execution has a tenant, and its events go only to its own Redis Stream,
`{REDIS_EVENT_STREAM}.{tenant_id}` (for example `executor.events.pr-agentic-service`). The tenant
must match `^[a-z][a-z0-9-]{1,62}[a-z0-9]$`, must not be `dlq` or `work`, never changes, and is
part of the submit idempotency fingerprint. Stream entries carry a `tenant_id` field; the payload
is unchanged. Detail and list responses expose `tenant_id`, and lists only ever cover the
caller's tenant. See [Redis execution events](dev_docs/redis-execution-events.md) for subscriber rules.

ExecutionSpec stays at schema version `1.0` during pre-release development. Each ordered Step uses
`PYTHON_EXECUTE` and independently embeds INLINE code or references one UTF-8 `.py` file below
`INPUT_STORAGE_ROOT` (defaults to `SHARED_STORAGE_ROOT`) with a root-relative path and SHA-256
(no implicit prefix). Executor persists the resolved source and
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

- The Agent chooses input directories below `INPUT_STORAGE_ROOT` and sends the full root-relative
  PATH of each `.py` file. Executor does not add `requests/` or another prefix. Executor reads
  them through its Agent/Executor shared volume; Jupyter does not need this volume.
- `SHARED_STORAGE_ROOT` is Executor's writable result root (source snapshots, outputs, manifests).
  `INPUT_STORAGE_ROOT` is the caller-authored input root and can be read-only to Executor.
  If omitted/empty, the input root uses `SHARED_STORAGE_ROOT`; otherwise no search/fallback
  to the result root is performed. Execution submit, MULTI Operation and Artifact PATH inputs
  all use the input root. INLINE behavior is unchanged.
- For separate directories on one PVC, configure `INPUT_STORAGE_ROOT=/mnt/data/agent` and
  `SHARED_STORAGE_ROOT=/mnt/data/executor`. Both must be accessible inside Executor.
  Agent reads all result and manifest references relative to its mount of the Executor directory,
  not its own authoring directory. Input files are never modified or automatically created by Executor.
- Jupyter creates execution workspaces, notebooks, artifacts, datasets, and manifests on its own
  shared storage. Targets within one Runtime pool share that storage; different pools may use
  different PVs.
- Mounting the same shared PVC and root-relative layout on every Jupyter target within a pool is an operator-owned deployment
  contract; Executor does not discover or manage PV/PVC identity.
- Executor never opens Jupyter files locally. PostgreSQL stores Runtime-relative paths and
  Jupyter-computed metadata/checksums; notebook content is read through an available Jupyter target.
- Runtime retry prefers the original target/kernel. Storage-only reads prefer that target but may
  fall back only to another enabled ACTIVE/DRAINING target of the same Runtime type and the
  Execution's persisted pool. This boundary applies to downloads and notebook/text reads and
  writes; no storage operation falls back across pools.

`EXECUTION_MAX_RUNTIME_SECONDS` defaults to seven days and starts when a worker first claims the
Execution; running past it ends `FAILED`/`EXECUTION_TIMEOUT` with `finalized_by: MAX_RUNTIME`.
An Execution no worker claims within `EXECUTION_QUEUE_TIMEOUT_SECONDS` (default 24 hours) ends
`FAILED`/`QUEUE_TIMEOUT`. Step and Operation timeouts above the maximum runtime are rejected with
422. Each MULTI request supplies `lifecycle.operation_wait_timeout_seconds`; its deadline is
reset after every Operation and never exceeds the total Execution deadline. When it passes, the
Executor ends the Execution from the last Operation: a success is finalized to `SUCCEEDED`, a
failure ends `FAILED` with that Operation's failure type (`finalized_by: WAIT_TIMEOUT`, or
`MAX_RUNTIME` at the total deadline). A missing retained session produces `RUNTIME_SESSION_LOST`.
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
reports `worker_accepting=false`, PostgreSQL work admission stops, and
active jobs may finish for up to `EXECUTION_DRAIN_TIMEOUT_SECONDS`. Only jobs still running after
that deadline enter the existing `WORKER_SHUTDOWN` cleanup and recovery path. `/healthz` remains a
process liveness check, while `/workerz` reports `STARTING`, `ACCEPTING`, `DRAINING`, or `STOPPED`,
the local active execution count, and the latest startup reconciliation summary.

For planned service maintenance, use `POST /api/v1/maintenance/drain` instead of stopping each
Worker locally. It persists `DRAINING` for all replicas, keeps submissions durably queued, and
allows existing Runtime sessions, cancellation, and cleanup to continue. Poll
`GET /api/v1/maintenance` until `safe_to_shutdown=true`, then use
`POST /api/v1/maintenance/activate` after deployment. These operator-only controls are REST APIs,
not MCP Tools. See [Executor Maintenance](docs/executor-maintenance.md).

To stop active work instead of waiting, create a durable `STOP_ACTIVE_EXECUTIONS` Maintenance Run
with `POST /api/v1/maintenance/runs`. The Run snapshots active Execution targets, reuses the normal
fenced cancellation path, survives Executor restarts through an expiring lease, and exposes summary
and cursor-paginated target lookup APIs. Ordinary queued work is retained.

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
durable Execution event timeline in `event_sequence` order and current Redis publication state
while its transport row is retained.
Agent Subscribers call it only to recover a detected sequence gap; normal contiguous Redis
delivery needs no history query. See the
[Agent Event Consumer Guide](dev_docs/agent-execution-event-consumer-guide.md). Frontends compose the current
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
Agent Asset ID. Artifact state is queried from PostgreSQL and is not published as a separate
integration event.

Agent-authored `.ipynb` files are not execution inputs. Agent-authored Python is supplied per Step,
either INLINE or as a `.py` PATH. Executor builds the Notebook document from executed Steps and
outputs, while Jupyter writes `notebooks/execution.ipynb` into its own shared storage. Agent-authored
text Artifacts can be materialized through `execution_artifact_create` or
`POST /api/v1/executions/{execution_id}/artifacts`; `DATASET` and `MODEL` are excluded from this
command and remain Runtime/Manifest-produced Artifact types. REPORT content is written below
`reports/` and may also be appended as a Markdown notebook cell.

## Runtime fleet management

`runtime_target_upsert` accepts a stable name, `runtime_type`, driver-owned `connection_config`,
pool, optional capacity, and credential. For the Jupyter driver, `connection_config` contains only
an HTTP(S) `endpoint`, while the credential is its token. A credential is required when creating a
target and optional when updating one. It is encrypted before persistence and never returned by
MCP or REST. Registration immediately probes the driver; only an enabled `ACTIVE` target is
eligible for scheduling. A target's `runtime_type` is immutable after creation; register a new
target name when introducing a different Runtime Driver.

The background health monitor repeats the probe at
`RUNTIME_HEALTH_POLL_INTERVAL_SECONDS` on one Pod per interval. Each target is probed on its own
task, so a hung Runtime does not delay the others, and a pass skips targets checked within the
last half interval. A failed health or kernel-profile probe makes the target
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
Runtime Driver reports additional environments. The code default is `default` and `3102311`.

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
Runtime Compose stack includes two BATCH servers for this behavior; capacity is configured per registered
target.

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
probe, drain, activate, disable, and a deliberately restricted hard purge. Purge takes only
`actor`, requires a disabled `OFFLINE` target without unfinished work or unconfirmed kernel
cleanup, and preserves historical Execution/Attempt UUID references and an immutable deletion
record. Repeating purge for the same UUID returns that record, even after same-name registration
with a new UUID. No execution history or files are deleted. See
[purge contract](dev_docs/post-runtime-target-purge.md).
Credentials are accepted only on upsert and credentials are absent from every response.
The non-secret endpoint is returned as `runtime.connection_config.endpoint`.

## Jupyter shared storage contract

Every Jupyter target mounts the local `./test_harness/jupyter/workspace` or production shared PVC
at its configured contents root. Executor does not mount it. Execution files use this stable
Jupyter-relative hierarchy:

```text
/workspace/pv/tenants/{tenant_id}/users/{user_id}/projects/{project_id}/sessions/{session_id}/executions/{execution_id}/
    ├── notebooks/execution.ipynb
    ├── artifacts/
    │   ├── plots/
    │   ├── models/
    │   ├── metrics/
    │   ├── reports/
    │   ├── logs/
    │   └── other/
    └── checkpoints/
```

Raw data remains in S3. PATH submissions are resolved under
`INPUT_STORAGE_ROOT` (or `SHARED_STORAGE_ROOT` when unset), and paths escaping that root are rejected.
All shared-result references, including source snapshots and output representations inside Step
manifests, use `SHARED_STORAGE_ROOT` instead. See
[Shared PV path contract](docs/shared-pv-path-contract.md) for the coordinated transition.
The reusable processed-data hierarchy is intentionally not fixed until
[Deferred Decisions](docs/deferred-decisions.md) DD-002 is resolved.

## Consistency and delivery

Submission and cancellation commit only PostgreSQL state; Workers find the work by polling
PostgreSQL (see [Event Delivery](docs/event-delivery.md#work-admission)). Public events begin when
Runtime execution actually starts and are written to the Outbox in the same transaction as the
state change. A background publisher claims pending rows with `FOR UPDATE SKIP LOCKED`, publishes
them to the Execution's `executor.events.{tenant_id}` Stream, and then marks them published. A crash between Redis `XADD` and the
database update can create a duplicate, so consumers must deduplicate on `event_id`. Executor
Workers never consume Agent integration events.

Active attempts renew a PostgreSQL lease. A MULTI Attempt in
`WAITING_FOR_OPERATION` releases its worker lease but keeps its session reservation, so it counts
against that Runtime Target's capacity. A background audit verifies retained sessions and enforces
both stored deadlines after Executor restarts. An expired active lease is failed safely and can be
retried by a later retry workflow; automatic re-execution is intentionally not enabled yet.

The retention manager bounds Executor-owned transport data on one Pod per interval. It
time-trims Agent events, deletes only old `PUBLISHED` Outbox rows, and retains public `execution_events` independently for gap recovery.
The Agent-owned Event DLQ is not modified by Executor. Retention intervals and batch size are
environment-configurable; see [Event Delivery](docs/event-delivery.md).

Every published Stream entry and JSON payload uses the versioned Executor event contract. Agent
consumers must use their own consumer group and durably deduplicate on `event_id` before ACK. See
[Redis Execution Event Contract 1.0](dev_docs/redis-execution-events.md) and the reference
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

Settings come from, highest first, environment variables, `.env` (local runs only, ignored by Git),
`config.{APP_ENV}.yml` (per environment) and `config.yml` (shared by every environment), both in
the working directory. `config.yml` is read whenever it exists. Deployments set `APP_ENV` to
`dev`, `stg` or `prd`, which requires the matching file and fails startup without it. Local runs
leave `APP_ENV` unset or empty and read only `config.yml`, `.env` and environment variables, so
local values go in `config.yml` or `.env`. `APP_ENV` is read from the process environment only,
never from `.env`. `config.yml` lists every shared tunable at its code default in
`src/executor_service/settings.py` (a test keeps the two equal, so change both together);
`config.dev.yml`, `config.stg.yml` and `config.prd.yml`
hold credentials, MCP host/origin allowlists, storage roots and other deployment-only values. Keys
are the setting names in any case. The
platform offers no ConfigMaps or Secrets, so these files, credentials included, ship in the
image. Build one image per environment with `--build-arg APP_ENV=<env>`; it then carries
`config.yml` and that environment's file only. `DATABASE_URL` and `REDIS_URL` are secret settings and are never
intentionally logged. `.env.example` contains local-only credentials.

`MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` are fail-closed allowlists for the SDK's DNS-rebinding
protection. Add the Kubernetes gateway hostname and origin before deployment; do not disable the
protection to make a proxy work. stg and prd set `MCP_ENABLED: false`, so `/mcp` is not mounted.

`AUTH_MODE` is `header` locally and in dev (identity headers trusted, tenant `dev`) and `api_key`
in stg and prd, where `AUTH_CLIENTS` lists each caller's key SHA-256, default tenant, relay
permission, and admin flag. Startup refuses `header` outside local and dev. The shipped
placeholder hashes match no key: replace them with `printf %s "$KEY" | sha256sum` output.

Redis carries only the Agent-facing `REDIS_EVENT_STREAM`. Workers find work by polling PostgreSQL
every second; `EXECUTION_WORKER_MAX_ACTIVE` caps the Executions one Pod runs at once so a backlog
costs a bounded number of claims and spreads across replicas.

`EVENT_RETENTION_*` configures the singleton cleanup cadence, lease, and bounded batch size.
`REDIS_EVENT_RETENTION_SECONDS` controls the event Stream window;
`PUBLISHED_OUTBOX_RETENTION_SECONDS` and `EXECUTION_EVENT_RETENTION_SECONDS` independently bound
transport rows and durable terminal-event history. Executor never trims the Agent-owned Event DLQ.
The event window applies to every tenant Stream recorded in PostgreSQL. Set `REDIS_EVENT_STREAM_MAXLEN` to also add `MAXLEN ~ <value>` to every event `XADD`
(per Stream, approximate). It is unset by default; `MINID` is not used because Redis 6.0 lacks it.

Each Worker names itself `<hostname>-<pid>-<suffix>` as its lease owner (shown on `/workerz`). See
[Multi-Executor Operations](docs/multi-executor-operations.md) for locking invariants, lease
recovery, deployment behavior, and the real crash E2E.

Set Kubernetes `terminationGracePeriodSeconds` greater than
`EXECUTION_DRAIN_TIMEOUT_SECONDS + EXECUTION_SHUTDOWN_CLEANUP_SECONDS` plus a shutdown buffer.
Executions longer than the configured drain window cannot move their live Jupyter WebSocket to
another Pod; they use the documented failure and explicit retry path when the deadline expires.

`RUNTIME_MAX_OUTPUT_MESSAGE_BYTES` bounds one incoming Runtime WebSocket message before JSON or
base64 decoding. The 32 MiB default is intended for local safety. Production must set it from T35
measurements and Pod concurrency/memory limits. A breach is explicit
`OUTPUT_LIMIT_EXCEEDED`; Executor interrupts the Runtime and marks the sealed Step result
`complete=false` rather than silently truncating it.

Executor starts with an empty Runtime Fleet. Every Jupyter endpoint and credential is registered
through the Runtime Target REST or MCP API and the credential is encrypted with
`RUNTIME_CREDENTIAL_KEY` before PostgreSQL storage. Plaintext credentials are not placed in request
URLs or responses. Rotate the encryption key only with a credential re-encryption procedure;
replacing it directly makes existing dynamic credentials unreadable.

## Kubernetes deployment

A Kubernetes Deployment, ClusterIP Service, release migration Job, environment ConfigMap, and
Secret key example are provided in [deploy/kubernetes](deploy/kubernetes/README.md). The baseline
mounts the Agent/Executor shared-result PVC read-write. Jupyter notebook and Artifact storage
remains attached to the Jupyter fleet and is accessed through authenticated Jupyter APIs.

## Package structure

```text
src/executor_service/
├── domain/           # entities, state rules, ports
├── application/      # submit/get/cancel use cases
├── infrastructure/   # SQLAlchemy, Redis Outbox, Runtime Drivers and fleet worker
└── interfaces/       # MCP SDK schemas/tools and HTTP host
```
