# Production readiness backlog

Hardening work required before production operation. Completed items keep one line and point to
the document that now describes the behaviour. Open items keep only their remaining criteria.

## Implemented

| Item | Summary | Current description |
|---|---|---|
| PR-001 | Fence stale Worker ownership with leases and fencing tokens | [Execution recovery](execution-recovery.md), [Multi-Executor operations](multi-executor-operations.md) |
| PR-002 | Stop and verify Runtime work after a timeout | [Execution recovery](execution-recovery.md) |
| PR-003 | Reconcile durable reservations with observed Runtime sessions | [Runtime Target operations](runtime-targets.md) |
| PR-005 | Controlled maintenance (DRAINING, `safe_to_shutdown`) and fail-safe restart recovery | [Executor maintenance](executor-maintenance.md) |
| PR-006A | Exclusive and recoverable cancellation ownership | [Execution recovery](execution-recovery.md) |
| PR-006B | Ordered and recoverable integration events through the transactional Outbox | [Event delivery](event-delivery.md) |
| PR-006C | Bounded Redis Streams decoupled from durable event history | [Event delivery](event-delivery.md), [Redis 6.0.8 compatibility](redis-6-compatibility.md) |

A Runtime-side Runner that keeps executing across Executor Worker loss stays out of scope. An
unexpected Worker loss is handled as a failure, never as a silently resumed execution.

## Open

### PR-004: Full Runtime output without unbounded Executor buffering

Status: IN_PROGRESS. Output is streamed to fenced shared result storage, PostgreSQL and Redis keep
bounded summaries and references, and a breach of `RUNTIME_MAX_OUTPUT_MESSAGE_BYTES` records
`OUTPUT_LIMIT_EXCEEDED` with `complete=false` ([Shared result storage](shared-result-storage.md),
[Runtime output completeness](runtime-output-completeness.md)).

Remaining:

- Run the full T35 matrix (`scripts/t35_output_measurement.py --preset full --confirm-full`, see
  [Executor resilience testing](executor-resilience-testing.md)) against the production Jupyter
  deployment.
- Select and record the production `RUNTIME_MAX_OUTPUT_MESSAGE_BYTES`. The checked-in 32 MiB is a
  conservative local default, not an approved threshold.

### PR-007: Mandatory and reproducible quality gates

Status: READY_FOR_CI_WIRING. The gate commands are in [Quality gates](quality-gates.md). The
repository does not assume GitHub Actions because deployment uses an internal CI/CD platform.

Remaining:

- Internal pull-request or build validation runs the default quality gate and blocks on failure.
- A required integration job provisions Redis and PostgreSQL and runs `--integration` with no
  skipped required cases.
- Release candidates run the real Jupyter validation level appropriate to the change and retain
  its summary and logs, with service credentials injected as secrets.

### PR-008: Trustworthy Runtime diagnostics and completion reporting

Status: IMPLEMENTING. Phases 1 to 10 are delivered
([Runtime diagnostics](runtime-diagnostics.md)).

Remaining:

- Keep the no-false-completeness rule across supported Runtime variants. Native Jupyter 2.20.0
  warnings are covered; unknown output loss and changed remote warning formats remain explicit
  compatibility limits. Do not treat arbitrary user stderr as authoritative.
- Execution failure, result incompleteness and secondary projection or cleanup problems stay
  independently attributable and queryable without overwriting earlier causes.
- Common diagnostic code, phase, origin, severity, time and scope rules cover DB, API, event and
  manifest representations with bounded payloads.
- Remaining lifecycle and persistence-outage paths, including prolonged storage or DB outages,
  are covered by failure-injection tests; unknown remote causes stay explicitly unknown.
