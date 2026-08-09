# Multi-Executor Operations

Executor supports multiple Kubernetes Pods sharing PostgreSQL, Redis Streams, the Jupyter
registry, and the shared PV. PostgreSQL is the execution source of truth; Redis only wakes a
Worker.

## Coordination invariants

- Every process uses the same `EXECUTION_CONSUMER_GROUP` and a unique
  `EXECUTION_CONSUMER_NAME`. Inject the Pod name in Kubernetes. The hostname and PID fallback is
  suitable for local execution but should not replace an explicit production identity.
- Claiming locks the Execution row. Duplicate Redis delivery or simultaneous reconciliation can
  therefore create only one running Attempt for an Execution.
- Server selection locks Jupyter server rows with `FOR UPDATE SKIP LOCKED`. The running/waiting
  Attempt count is checked while that lock is held, so concurrent Pods cannot exceed registered
  server capacity.
- Reconciliation intentionally runs in every Pod. Losing the Redis notification does not lose the
  Execution because an unsuccessful claim leaves it durably `QUEUED`.
- A Redis message is acknowledged after dispatch, not after a multi-day execution completes.
  PostgreSQL status, leases, and reconciliation own the long-running lifecycle.

## Graceful shutdown and process crash

On graceful shutdown, the owning Worker cancels its local handlers, classifies unfinished STATIC
work as `WORKER_SHUTDOWN`, deletes its kernel, and exposes a `FROM_START` retry when safe. A forced
process or Pod failure cannot perform that cleanup. Another Pod detects the expired lease,
transitions the Execution to `FAILED` with `LEASE_EXPIRED`, and then deletes the abandoned kernel.

Kernel cleanup is intentionally observable as a short two-stage transition:

1. `FAILED` with `kernel_cleanup_status=PENDING`;
2. `FAILED` with `kernel_cleanup_status=SUCCEEDED` or `FAILED`.

`execution_retry` rejects a FROM_START retry while cleanup is `PENDING`. Automatic retry is not
performed; Agent/API must explicitly request it after inspecting the failure and cleanup result.

Set `EXECUTION_HEARTBEAT_SECONDS` comfortably below `EXECUTION_LEASE_SECONDS`. The current
validation requires at least 5 seconds and 30 seconds respectively. Detection can take roughly one
lease duration plus one heartbeat polling interval after the last successful heartbeat.

## Deployment rules

- Run Alembic once as a release or init job, not independently in every application Pod.
- All Pods must use the same PostgreSQL database, Redis Stream/group, credential encryption key,
  workspace PV, and Jupyter registry.
- Do not reuse an explicit consumer name across concurrently running Pods.
- A rolling restart can fail an in-flight job with `WORKER_SHUTDOWN`; it does not transparently
  migrate a live Jupyter WebSocket between Pods. Drain traffic and plan explicit retries for
  long-running work before deployment.
- Prometheus Worker gauges are process-local and should be aggregated by Pod. Execution Attempt
  history exposes the `lease_owner` responsible for each attempt.

## Verification

Run the real PostgreSQL concurrency suite. It creates and drops an isolated temporary database:

```bash
docker compose up -d --wait postgres redis
EXECUTOR_RUN_POSTGRES_TESTS=1 uv run pytest tests/test_multi_worker_postgres.py
```

Run the crash failover smoke test with the primary Jupyter server healthy. The script starts two
Executor processes on ports `8010` and `8011`, submits a long job to the first, kills that process
with `SIGKILL`, and verifies that the second records lease expiry, cleans the kernel, and completes
exactly one explicit retry:

```bash
docker compose up -d --wait postgres redis jupyter
uv run python scripts/multi_executor_failover_smoke.py
```

Override `MULTI_EXECUTOR_PRIMARY_PORT` and `MULTI_EXECUTOR_SECONDARY_PORT` if those ports are in use.
