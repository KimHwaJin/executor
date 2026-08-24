# Execution failure and recovery

PostgreSQL is the source of truth for recovery state. Redis Streams wakes workers, but a missing or
duplicate wake-up does not decide an Execution outcome. Workers claim `QUEUED` rows transactionally,
renew leases while a Jupyter cell is running, and reconcile expired leases.

## Failure classification

`execution_get` and Attempt history expose failure, retry, and Runtime session cleanup state.

| Failure type | Meaning | Default retry |
|---|---|---|
| `TOOL_ERROR` | A Runtime step returned a code error | `FROM_FAILED_STEP` while its session is retained |
| `RUNTIME_UNAVAILABLE` | The assigned Runtime Driver/Target became unavailable | `FROM_START` with a new session |
| `RUNTIME_SESSION_LOST` | The assigned retained Runtime session no longer exists | `FROM_START` only after an explicit retry |
| `WORKER_SHUTDOWN` | Executor stopped while a step was running | `FROM_START` with a new session |
| `LEASE_EXPIRED` | A running Worker stopped renewing its PostgreSQL lease | `FROM_START` with a new session |
| `INTERNAL_ERROR` | Executor validation or internal processing failed | `NOT_RETRYABLE` until reviewed |
| `INFRASTRUCTURE_ERROR` | Reserved for non-Jupyter infrastructure failures | Policy is assigned at the failure site |
| `STEP_TIMEOUT` | One Step exceeded `step_timeout_seconds` | `FROM_FAILED_STEP` when the session is retained |
| `OPERATION_TIMEOUT` | Total Operation time exceeded `operation_timeout_seconds` | `FROM_FAILED_STEP` when the session is retained |
| `OPERATION_WAIT_TIMEOUT` | A MULTI caller did not submit/finalize before its wait deadline | `NOT_RETRYABLE` |

A successful or cancelled Execution has no `failure_type` and uses `NOT_RETRYABLE`.
`retry_strategy` is the single source of truth for whether and how an Execution can be retried.
Explicit retry is currently restricted to `SINGLE`. MULTI Tool failures return to
`WAITING_FOR_OPERATION` and accept a correction Operation. MULTI Runtime-state loss remains
non-retryable because a new kernel cannot reconstruct prior in-memory cell state safely.

Step and Operation timeouts run a bounded Runtime abort workflow. Executor first records
`runtime_abort_status=PENDING`, requests a driver-specific abort, and waits up to
`RUNTIME_ABORT_TIMEOUT_SECONDS` for positive idle confirmation. Only `IDLE_CONFIRMED` permits
same-session `FROM_FAILED_STEP` recovery or a MULTI correction Operation. If the session is missing
or idle cannot be confirmed, Executor deletes it and records `SESSION_MISSING` or
`SESSION_DELETED`; recovery can then start only from sequence zero where policy permits it. If
deletion fails, both abort and cleanup are `FAILED`, the Runtime reservation remains, and retry is
blocked until maintenance cleanup succeeds.

For an in-flight user cancellation, the interrupted execution job only preserves files written by
the current cell as `INCOMPLETE` evidence. The replacement cancellation job exclusively interrupts
and deletes the Runtime session and commits the `CANCELLED` state and event. Worker shutdowns that
do not originate from `CANCEL_REQUESTED` remain owned by the execution job and are classified as
`WORKER_SHUTDOWN`.

## Retry strategies

- `FROM_FAILED_STEP` requires the same retained session, Runtime Target, and an unexpired retention
  window. Successful predecessor Steps remain unchanged. The retry creates a new Attempt and starts
  at `retry_from_sequence`.
- `FROM_START` clears the stale session and target assignment, resets every Step to `PENDING`, selects
  an eligible target again, starts a new session, and executes from sequence zero.
- `NOT_RETRYABLE` rejects `execution_retry`.

Both SINGLE strategies retry the same accepted Operation rather than creating another one. The
Operation returns from `FAILED` to `QUEUED`, then records the latest terminal result and current
Attempt ID. Immutable Attempt and Step Attempt rows preserve every previous try. Consequently a
single Operation can have multiple terminal Outbox events; their `event_id` values identify the
individual notifications.

If a retained target is temporarily `OFFLINE` between the retry request and Worker claim, Executor
keeps the Execution `QUEUED` and pinned to that target and session. A recovered `ACTIVE` or
operator-controlled `DRAINING` target can resume the failed Step. Executor confirms the retained
session still exists before executing code. A missing session records `RUNTIME_SESSION_LOST` with a
`FROM_START` strategy, but does not automatically run on another target; the caller must explicitly
retry. A missing or disabled target similarly records `RUNTIME_UNAVAILABLE` and requires an
explicit `FROM_START` retry. If the retention window expires while the target is unavailable, the
queued retry returns to `FAILED`, session cleanup is attempted, and
`execution.retry_window_expired` is emitted.

## Worker shutdown and lease expiry

During application shutdown the Worker first stops Redis intake and queue reconciliation, rejects
new claims, and waits up to `EXECUTION_DRAIN_TIMEOUT_SECONDS` for its active jobs to finish. Their
heartbeats continue during this drain window. If the jobs finish, no shutdown failure is recorded.

After the drain deadline, each remaining in-flight job is cancelled, attempts to interrupt and
delete its session, records a `WORKER_SHUTDOWN` failure, and commits the Attempt and Outbox event
before database disposal. Kernel cleanup is bounded by `EXECUTION_SHUTDOWN_CLEANUP_SECONDS` (20
seconds by default), so an unresponsive Jupyter server does not prevent the database failure state
from being committed.

An ungraceful process loss cannot run that shutdown path. The surviving Worker that locks an expired
lease records `LEASE_EXPIRED`, increments `recovery_count`, marks the retry strategy `FROM_START`,
and performs best-effort abandoned-session deletion. Re-running reconciliation is idempotent because
only `RUNNING` rows with expired leases are eligible.

## Runtime session cleanup

`runtime_session_cleanup_status` is one of:

- `NOT_REQUIRED`: there was no session to clean up, or a Tool-error session is intentionally retained;
- `PENDING`: lease recovery committed and cleanup is about to run;
- `SUCCEEDED`: the driver accepted session deletion and the current Execution session ID is cleared;
- `FAILED`: cleanup could not be confirmed; the historical Attempt retains its session ID.

Lease recovery emits `execution.runtime_session_cleanup_completed` or
`execution.runtime_session_cleanup_failed` after the cleanup result is persisted. Expired retained-session
windows emit `execution.retry_window_expired`.

`runtime_abort_status` is one of `NOT_REQUIRED`, `PENDING`, `IDLE_CONFIRMED`, `SESSION_DELETED`,
`SESSION_MISSING`, or `FAILED`. It is stored on both the current Execution and immutable Attempt
history. `execution.runtime_abort_started`, `execution.runtime_abort_completed`, and
`execution.runtime_abort_failed` make the bounded outcome observable without carrying code or
output. Lease recovery and cancellation resolve a previously `PENDING` abort rather than leaving
an unknown state indefinitely.

## Long-running cells

`JUPYTER_REQUEST_TIMEOUT_SECONDS` applies only to Jupyter REST operations such as health checks and
kernel creation. Cell execution uses the Jupyter WebSocket without an application-level receive
deadline, so a five-day cell is not cancelled by the 30-second REST timeout. WebSocket ping/pong
still detects a broken connection, which becomes `RUNTIME_UNAVAILABLE`.

The Executor heartbeat runs in a separate asynchronous task and continues to renew the PostgreSQL
lease while `execute_cell` is waiting for Jupyter output.

## Verification

Unit tests cover Tool and infrastructure retry state, Attempt read models, and idempotent lease
reconciliation. Local real-Jupyter tests are:

```bash
uv run python scripts/jupyter_retry_smoke.py
uv run python scripts/jupyter_retry_offline_recovery_smoke.py
uv run python scripts/jupyter_worker_recovery_smoke.py
JUPYTER_GATEWAY_ENDPOINT=http://127.0.0.1:8888 \
JUPYTER_GATEWAY_TOKEN=change-me-local-only \
uv run python scripts/jupyter_timeout_abort_smoke.py
```

The OFFLINE recovery smoke temporarily replaces the registered server endpoint with an unreachable
address, verifies the retry remains queued with the original server and kernel IDs, restores the
endpoint, and verifies the failed Step resumes on that kernel.

The Worker recovery smoke starts a long cell, stops the Worker, verifies classified cleanup, starts
a new Worker, retries from sequence zero, and checks immutable Attempt history.
