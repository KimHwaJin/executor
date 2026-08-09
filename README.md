# Executor Service

Asynchronous Jupyter execution control plane exposed as an MCP 2026-07-28 Streamable HTTP server.
PostgreSQL is the source of truth. Tool calls persist work and return immediately while a Redis
consumer worker executes STATIC plans in Jupyter.

## Implemented scope

- Official MCP Python SDK 2.x `MCPServer`, exposed at `POST /mcp`
- Execution tools: `executor_get_capabilities`, `execution_submit`, `execution_get`,
  `execution_cancel`, `execution_retry`, `execution_attempt_list`, `execution_event_list`,
  `execution_trace_get`, `execution_artifact_list`, `execution_artifact_get`
- Jupyter fleet tools: `jupyter_server_upsert`, `jupyter_server_list`,
  `jupyter_server_get`, `jupyter_server_probe`, `jupyter_server_remove`,
  `jupyter_server_set_state`
- Execution, ExecutionStep, and OutboxEvent persistence with SQLAlchemy 2 and Alembic
- Transactional Outbox publisher with at-least-once Redis Stream delivery
- Redis consumer group worker with PostgreSQL reconciliation
- Jupyter REST/WebSocket kernel execution, interrupt, and deletion
- Multi-server Jupyter registry, encrypted credentials, health probes, capacity scheduling,
  execution attempts, leases, and heartbeats
- Safe server draining and retained-kernel retry from a failed Step
- Immutable per-Attempt Step history and an end-to-end execution event trace
- Automatic and Manifest-based Artifact registration with checksum and lineage
- Durable `.ipynb` output and execution-scoped artifact directories on the shared PV
- `/healthz`, `/readyz`, and Prometheus `/metrics`
- PostgreSQL, Redis, and `jupyter/datascience-notebook` through Docker Compose

MCP Tasks are deliberately not used. `execution_submit` returns an `execution_id` while the
execution starts as `QUEUED`. Poll with `execution_get` or request cancellation with
`execution_cancel`. Actual Jupyter execution currently supports STATIC mode; DYNAMIC is retained
in the contract for the later Agent re-planning loop and is reported separately in capabilities.

## Deferred decisions

Return-value materialization, reusable Asset promotion, and user-versus-project Asset visibility
are intentionally not implemented yet. Their agreed constraints, open questions, and resume
criteria are tracked in [Deferred Decisions](docs/deferred-decisions.md). Update that decision log
before implementing or changing any deferred behavior.

Arize Phoenix tracing is also planned but not implemented. Local integration tests for that future
feature will use the already available `arizephoenix/phoenix:nightly` image; the validated Compose
and OTLP configuration will be added with the tracing feature rather than guessed in advance.

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

To run a second Jupyter server against the same local PV:

```bash
docker compose --profile multi-jupyter up -d --wait
```

Operational endpoints:

- MCP: `http://127.0.0.1:8000/mcp`
- liveness: `http://127.0.0.1:8000/healthz`
- readiness (PostgreSQL, Redis, Jupyter): `http://127.0.0.1:8000/readyz`
- Prometheus: `http://127.0.0.1:8000/metrics`

Run the official SDK client smoke test in a second terminal:

```bash
uv run python scripts/mcp_smoke.py
uv run python scripts/jupyter_gateway_smoke.py
uv run python scripts/jupyter_execution_smoke.py
uv run python scripts/jupyter_cancel_smoke.py
uv run python scripts/jupyter_failure_smoke.py
uv run python scripts/jupyter_fleet_smoke.py
uv run python scripts/jupyter_retry_smoke.py
uv run python scripts/jupyter_drain_smoke.py
uv run python scripts/jupyter_artifact_smoke.py
```

## Quality checks

```bash
uv run ruff check .
uv run ty check
uv run pytest
```

Migration checks:

```bash
uv run alembic current
uv run alembic downgrade base
uv run alembic upgrade head
```

## Tool contracts

`execution_submit` accepts one `request` object. Important fields are:

- `idempotency_key`: required for safe retries; reuse with different content is rejected
- `mode`: `STATIC` or `DYNAMIC`
- `trigger_type`: `INTERACTIVE` or `BATCH`
- `jupyter_pool`: `INTERACTIVE` or `BATCH`
- `kernel_name`: one of the deployment's configured kernels
- `source`: either `{ "type": "INLINE", "code": "..." }` or
  `{ "type": "PATH", "path": "/shared/..." }`
- `context`: Agent-owned user/project/session/plan IDs; Executor creates `execution_id`
- `steps`: ordered execution units with optional skill and tool names

`execution_cancel` also requires an idempotency key. It first records `CANCEL_REQUESTED`; the
worker then interrupts and deletes the kernel before recording `CANCELLED`.

`execution_retry` is accepted only for a `FAILED` execution marked `retryable`. A notebook cell
error preserves that kernel for `FAILED_KERNEL_RETENTION_SECONDS` and reports
`retry_from_sequence` and `retained_kernel_until`. Retry creates a new ExecutionAttempt and resumes
the failed cell on the same server and kernel, preserving successful predecessor Step states and
outputs. Infrastructure failures that cannot guarantee kernel state are not retryable. A retained
kernel counts against server capacity and is deleted automatically when its retry window expires.

`execution_attempt_list` returns every worker Attempt in order, including the selected Jupyter
server, kernel, lease/heartbeat times, outcome, and only the Steps actually run by that Attempt.
Each Step history row snapshots its skill, tool, inputs, outputs, error, and timestamps, so a retry
does not overwrite evidence from the earlier failure. `execution_event_list` returns the
transactional Outbox timeline and current Redis publication state. `execution_trace_get` combines
the current Execution, Attempt/Step histories, and events for an end-to-end frontend detail view.
Secret-shaped keys in historical inputs, outputs, and event payloads are defensively redacted.

Execution-scoped files created or modified under `artifacts/` and `reports/` are detected after
each Step. Successful files are `AVAILABLE`; files left by a failed cell are `INCOMPLETE`, so a
later retry produces a separate Attempt-linked Artifact rather than overwriting the failure
evidence. The final `.ipynb` is registered after successful execution. PV size and SHA-256 are
computed by Executor.

Tools can append JSON Lines to `artifacts/manifest.jsonl` to register user-level processed data or
S3 objects outside the execution workspace. Manifest use is optional and does not require every
analysis Tool to accept an Asset ID. S3 metadata and checksum are caller-declared because Executor
does not read the object. See [Artifact Manifest](docs/artifact-manifest.md) for the contract.

`execution_artifact_list` and `execution_artifact_get` expose execution/Attempt/Step references,
storage URI, media type, size, checksum, status, metadata, and a direct parent Artifact or external
Agent Asset ID. `execution_trace_get` includes the same Artifact collection. Registration emits
`execution.artifact_registered` through the Transactional Outbox.

Python files may use `# %%` markers to define notebook cells. Existing `.ipynb` PATH inputs use
their code cells. When explicit planned steps are supplied, their count must match the executable
cell count. If steps are omitted, the worker creates one ExecutionStep per cell.

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

Use `jupyter_server_set_state` with `DRAINING` before server maintenance. Existing executions and
retained retry kernels remain attached, while new work is excluded from that server. The response
sets `drain_complete=true` after its active/reserved count reaches zero. `ACTIVE` probes the server
before allowing new work again; `remove` is the separate operation for durable disablement.

## Shared PV contract

The local bind mount is `./notebook_dir:/workspace/pv`. Kubernetes should mount the shared PVC at
the same in-container root. Execution files use the following stable hierarchy:

```text
/workspace/pv/users/{user_id}/
├── datasets/processed/{asset_id}/
└── projects/{project_id}/sessions/{session_id}/executions/{execution_id}/
    ├── code/
    ├── notebooks/execution.ipynb
    ├── artifacts/
    ├── reports/
    └── checkpoints/
```

Raw data remains in S3. PATH submissions are resolved under the configured PV root and path
traversal is rejected.

## Consistency and delivery

Submission and its `execution.submitted` OutboxEvent are committed in one PostgreSQL transaction.
Cancellation and `execution.cancel_requested` work the same way. A background publisher claims
pending rows with `FOR UPDATE SKIP LOCKED`, adds each event to the configured Redis Stream, and
then marks it published. A crash between Redis `XADD` and the database update can create a
duplicate, so consumers must deduplicate on `event_id`.

The consumer group treats Redis as a wake-up channel and reconciles `QUEUED` and
`CANCEL_REQUESTED` rows from PostgreSQL, so an acknowledged or lost notification does not lose the
execution. Active attempts renew a PostgreSQL lease. An expired lease is failed safely and can be
retried by a later retry workflow; automatic re-execution is intentionally not enabled yet.

No database migration runs automatically during service startup. Deployments must run Alembic as
a release or init job before readiness can pass.

## Configuration and secrets

All settings use environment variables; `.env` is ignored by Git. `DATABASE_URL` and `REDIS_URL`
are represented as secret settings and are never intentionally logged. `.env.example` contains
local-only credentials. Inject production values through the Kubernetes secret mechanism.

`MCP_ALLOWED_HOSTS` and `MCP_ALLOWED_ORIGINS` are fail-closed allowlists for the SDK's DNS-rebinding
protection. Add the Kubernetes gateway hostname and origin before deployment; do not disable the
protection to make a proxy work.

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
