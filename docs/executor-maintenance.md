# Executor Maintenance

Executor-wide maintenance admission is stored in PostgreSQL. It is different from both a local
Worker shutdown drain and a Runtime Target drain:

- Executor maintenance drain blocks new Runtime allocation across every Executor Pod.
- Local Worker drain is an internal process-shutdown state exposed by `/workerz`.
- Runtime Target drain removes one registered Runtime server from new scheduling.

## REST API

Maintenance operations are operator-facing REST endpoints and are not exposed as MCP Tools.

```text
GET  /api/v1/maintenance
POST /api/v1/maintenance/drain
POST /api/v1/maintenance/activate
```

Both mutations require the standard `idempotency_key` and `actor` body:

```json
{
  "idempotency_key": "maintenance-drain-20260826-1",
  "actor": {"type": "USER", "id": "operator-100"}
}
```

The response groups the persistent admission state, PostgreSQL workload counts, Runtime cleanup
counts, audit fields, and `safe_to_shutdown`:

```json
{
  "admission": {
    "state": "DRAINING",
    "accepting_new_executions": false,
    "version": 1
  },
  "workload": {
    "queued_execution_count": 2,
    "active_execution_count": 0,
    "cancel_requested_count": 0
  },
  "cleanup": {
    "unresolved_cleanup_count": 0,
    "active_runtime_session_count": 0
  },
  "active_run": null,
  "safe_to_shutdown": true,
  "created_by_type": null,
  "created_by": null,
  "updated_by_type": "USER",
  "updated_by": "operator-100",
  "created_at": "2026-08-26T00:00:00Z",
  "updated_at": "2026-08-26T00:01:00Z"
}
```

Queued Executions do not make shutdown unsafe because their specifications and state are durable
and no Runtime is allocated. Active, cancellation-in-progress, unresolved cleanup, or an owned
Runtime session makes `safe_to_shutdown=false`.

## Admission behavior

Drain does not reject `execution_submit`. A submitted Execution remains `QUEUED`; every Worker
checks and share-locks the singleton maintenance row in the same transaction that claims a new
Execution. Therefore, after the drain response commits, no Worker can begin a new Runtime
allocation. Redis notifications may be acknowledged because PostgreSQL remains the source of
truth and reconciliation resumes the queued work after activation.

Already-running work is not interrupted. A MULTI Execution that already owns a Runtime session may
continue or finalize while admission is draining. Cancellation and Runtime cleanup also remain
available.

Global drain deliberately does not make `/readyz` fail. The API must remain reachable for status,
cancellation, and activation. `/readyz` fails for a local process shutdown drain; `/workerz`
continues to describe only that local Worker.

## Current boundary

Maintenance Runs provide durable asynchronous stopping of active work:

```text
POST /api/v1/maintenance/runs
GET  /api/v1/maintenance/runs/{maintenance_run_id}
GET  /api/v1/maintenance/runs/{maintenance_run_id}/targets
```

Create a Run with the supported action:

```json
{
  "idempotency_key": "maintenance-stop-20260826-1",
  "action": "STOP_ACTIVE_EXECUTIONS",
  "actor": {"type": "USER", "id": "operator-100"}
}
```

Creation atomically changes global admission to `DRAINING`, captures the active Execution IDs, and
returns `202 Accepted` with a `maintenance_run_id` and target counts. Ordinary `QUEUED` Executions
without an owned Runtime session are not selected and remain queued for a later activation.

Every Run and target is stored in PostgreSQL. One Worker owns a Run through an expiring lease and
monotonic fencing token. It sends each target through the normal idempotent Execution cancellation
state machine, then observes cancellation and Runtime cleanup until all targets are stopped. If the
Worker disappears, another Worker claims the expired Run lease and continues only its unfinished
targets. This resumes the stop workflow; it does not resume interrupted user code.

Only one non-terminal Maintenance Run is allowed at a time. Target listing uses opaque cursor
pagination so a Run may safely contain many thousands of Executions. A Run remains `RUNNING` while
Runtime cleanup is unresolved; background cleanup and Run reconciliation can later complete it.
`GET /api/v1/maintenance` includes the current `active_run` reference so operators can rediscover
the Run ID after losing a client response or restarting Executor.

Unexpected-Worker-loss classification and startup reconciliation outside an explicitly requested
Maintenance Run remain the next PR-005 phase.
