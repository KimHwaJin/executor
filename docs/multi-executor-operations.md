# Multi-Executor Operations

Executor supports multiple Kubernetes Pods sharing PostgreSQL, Redis Streams, and the Jupyter
registry. The Jupyter fleet—not Executor—mounts Jupyter shared storage. PostgreSQL is the execution
source of truth; Redis only wakes a Worker.

## Coordination invariants

- Every process uses the same `EXECUTION_CONSUMER_GROUP` and a unique
  `EXECUTION_CONSUMER_NAME`. Inject the Pod name in Kubernetes. The hostname and PID fallback is
  suitable for local execution but should not replace an explicit production identity.
- Claiming locks the Execution row. Duplicate Redis delivery or simultaneous reconciliation can
  therefore create only one running Attempt for an Execution.
- Target selection locks Runtime Target rows with `FOR UPDATE SKIP LOCKED`. The running/waiting
  Attempt count is checked while that lock is held, so concurrent Pods cannot exceed registered
  target capacity.
- Reconciliation intentionally runs in every Pod. Losing the Redis notification does not lose the
  Execution because an unsuccessful claim leaves it durably `QUEUED`.
- Cancellation uses a separate PostgreSQL owner, expiry, heartbeat, and monotonic fence. Duplicate
  notifications and reconciliation candidates cannot make two Pods own Runtime cleanup for one
  Execution. After lease expiry, exactly one replacement Pod can continue the idempotent cleanup.
- A Redis message is acknowledged after dispatch, not after a multi-day execution completes.
  PostgreSQL status, leases, and reconciliation own the long-running lifecycle.
- Executor-wide admission is a singleton PostgreSQL state. A new claim takes a shared lock on that
  row, while drain or activate takes an exclusive lock, so the drain response cannot race with a
  late claim in another Pod.

## Graceful shutdown and process crash

Operator-requested global drain is separate from the local shutdown behavior below. Use
`POST /api/v1/maintenance/drain` to stop new Runtime allocation across every Pod while keeping the
service ready for queries, cancellation, and later activation. Submissions remain queued and
existing MULTI Runtime sessions may continue. See [Executor Maintenance](executor-maintenance.md).

`STOP_ACTIVE_EXECUTIONS` creates one durable Maintenance Run with one target row per selected
Execution. Workers compete for the Run with `FOR UPDATE SKIP LOCKED`; the winner holds an expiring
lease and fencing token. Cancellation uses a stable Run/Execution idempotency key. After Worker
loss, one replacement resumes unfinished targets after lease expiry while stale Run writes are
rejected.

On graceful shutdown, the owning Worker first enters `DRAINING`: it rejects new claims, cancels its
Redis intake and queue reconciliation loops, and makes `/readyz` fail through the
`worker_accepting` check. Already-dispatched jobs keep their heartbeats and may finish for up to
`EXECUTION_DRAIN_TIMEOUT_SECONDS`. Other Pods continue claiming queued work from the shared
PostgreSQL state.

When the drain deadline expires, remaining local handlers are cancelled. Unfinished SINGLE work is
classified as `WORKER_SHUTDOWN`, its Runtime session is deleted, and a `FROM_START` retry is exposed when
safe. A MULTI Step interrupted during shutdown is not replayed automatically. `/workerz` exposes
the local lifecycle state and active execution count for diagnosis.

A forced process or Pod failure cannot perform the shutdown cleanup. Another Pod detects the
expired lease, transitions the Execution to `FAILED` with `LEASE_EXPIRED`, and then deletes the
abandoned Runtime session.

For `CANCEL_REQUESTED`, the cancellation Worker owns a separate expiring lease. Claiming it
invalidates the execution fence before Runtime interruption begins. If that Worker disappears,
another Pod takes over after expiry; a stale owner cannot commit `CANCELLED` or publish its result.

Runtime session cleanup is intentionally observable as a short two-stage transition:

1. `FAILED` with `runtime_session_cleanup_status=PENDING`;
2. `FAILED` with `runtime_session_cleanup_status=SUCCEEDED` or `FAILED`.

`execution_retry` rejects a FROM_START retry while cleanup is `PENDING`. Automatic retry is not
performed; Agent/API must explicitly request it after inspecting the failure and cleanup result.

Set `EXECUTION_HEARTBEAT_SECONDS` comfortably below `EXECUTION_LEASE_SECONDS`. The current
validation requires at least 5 seconds and 30 seconds respectively. Detection can take roughly one
lease duration plus one heartbeat polling interval after the last successful heartbeat.

## Deployment rules

- Run Alembic once as a release or init job, not independently in every application Pod.
- All Pods must use the same PostgreSQL database, Redis Stream/group, credential encryption key,
  workspace PV, and Runtime Target registry.
- Do not reuse an explicit consumer name across concurrently running Pods.
- Set `terminationGracePeriodSeconds` greater than
  `EXECUTION_DRAIN_TIMEOUT_SECONDS + EXECUTION_SHUTDOWN_CLEANUP_SECONDS` plus a shutdown buffer.
- A rolling restart allows in-flight work to finish within the drain window, but it does not
  transparently migrate a live Jupyter WebSocket between Pods. Work exceeding the drain window can
  still fail with `WORKER_SHUTDOWN` and requires an explicit retry where supported.
- Execution Attempt history exposes the `lease_owner` responsible for each attempt.

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

Run the graceful SIGTERM handoff and 30-execution distributed-capacity scenarios with:

```bash
uv run python scripts/multi_executor_drain_smoke.py
uv run python scripts/multi_executor_load_smoke.py
```

Redis pause and Jupyter OFFLINE recovery scenarios, safety opt-ins, diagnostic queries, and the
validated baseline are documented in
[Executor Resilience Testing](executor-resilience-testing.md).
