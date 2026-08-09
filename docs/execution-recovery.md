# Execution failure and recovery

PostgreSQL is the source of truth for recovery state. Redis Streams wakes workers, but a missing or
duplicate wake-up does not decide an Execution outcome. Workers claim `QUEUED` rows transactionally,
renew leases while a Jupyter cell is running, and reconcile expired leases.

## Failure classification

`execution_get`, Attempt history, and `execution_trace_get` expose `failure_type`,
`retry_strategy`, and `kernel_cleanup_status`.

| Failure type | Meaning | Default retry |
|---|---|---|
| `TOOL_ERROR` | The Jupyter cell returned a code error | `FROM_FAILED_STEP` while its kernel is retained |
| `JUPYTER_UNAVAILABLE` | Jupyter REST or kernel WebSocket became unavailable | `FROM_START` with a new kernel |
| `WORKER_SHUTDOWN` | Executor stopped while a cell was running | `FROM_START` with a new kernel |
| `LEASE_EXPIRED` | A running Worker stopped renewing its PostgreSQL lease | `FROM_START` with a new kernel |
| `INTERNAL_ERROR` | Executor validation or internal processing failed | `NOT_RETRYABLE` until reviewed |
| `INFRASTRUCTURE_ERROR` | Reserved for non-Jupyter infrastructure failures | Policy is assigned at the failure site |

A successful or cancelled Execution has no `failure_type` and uses `NOT_RETRYABLE`.
The legacy `retryable` boolean remains in the MCP response for compatibility and is derived from
whether the strategy is different from `NOT_RETRYABLE`.

## Retry strategies

- `FROM_FAILED_STEP` requires the same retained kernel, Jupyter server, and an unexpired retention
  window. Successful predecessor Steps remain unchanged. The retry creates a new Attempt and starts
  at `retry_from_sequence`.
- `FROM_START` clears the stale kernel and server assignment, resets every Step to `PENDING`, selects
  an eligible server again, starts a new kernel, and executes from sequence zero.
- `NOT_RETRYABLE` rejects `execution_retry`.

If a retained server becomes unavailable between the retry request and Worker claim, Executor
downgrades that retry safely to `FROM_START` rather than attaching to an unknown kernel state.

## Worker shutdown and lease expiry

During application shutdown the Worker stops its Redis and reconciliation loops before cancelling
in-flight jobs. Each cancelled in-flight job attempts to interrupt and delete its kernel, records a
`WORKER_SHUTDOWN` failure, and commits the Attempt and Outbox event before database disposal.
Kernel cleanup is bounded by `EXECUTION_SHUTDOWN_CLEANUP_SECONDS` (20 seconds by default), so an
unresponsive Jupyter server does not prevent the database failure state from being committed.

An ungraceful process loss cannot run that shutdown path. The surviving Worker that locks an expired
lease records `LEASE_EXPIRED`, increments `recovery_count`, marks the retry strategy `FROM_START`,
and performs best-effort abandoned-kernel deletion. Re-running reconciliation is idempotent because
only `RUNNING` rows with expired leases are eligible.

## Kernel cleanup

`kernel_cleanup_status` is one of:

- `NOT_REQUIRED`: there was no kernel to clean up, or a Tool-error kernel is intentionally retained;
- `PENDING`: lease recovery committed and cleanup is about to run;
- `SUCCEEDED`: Jupyter accepted kernel deletion and the current Execution kernel ID is cleared;
- `FAILED`: cleanup could not be confirmed; the historical Attempt retains its kernel ID.

Lease recovery emits `execution.kernel_cleanup_completed` or
`execution.kernel_cleanup_failed` after the cleanup result is persisted. Expired retained-kernel
windows emit `execution.retry_window_expired`.

## Long-running cells

`JUPYTER_REQUEST_TIMEOUT_SECONDS` applies only to Jupyter REST operations such as health checks and
kernel creation. Cell execution uses the Jupyter WebSocket without an application-level receive
deadline, so a five-day cell is not cancelled by the 30-second REST timeout. WebSocket ping/pong
still detects a broken connection, which becomes `JUPYTER_UNAVAILABLE`.

The Executor heartbeat runs in a separate asynchronous task and continues to renew the PostgreSQL
lease while `execute_cell` is waiting for Jupyter output.

## Verification

Unit tests cover Tool and infrastructure retry state, Attempt read models, and idempotent lease
reconciliation. Local real-Jupyter tests are:

```bash
uv run python scripts/jupyter_retry_smoke.py
uv run python scripts/jupyter_worker_recovery_smoke.py
```

The Worker recovery smoke starts a long cell, stops the Worker, verifies classified cleanup, starts
a new Worker, retries from sequence zero, and checks immutable Attempt history.
