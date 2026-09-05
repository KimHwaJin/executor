# Operational hardening review follow-up

Baseline: `2ea06b0` on `feature/redis-6-0-compatibility`.
Implementation branch: `feature/executor-operational-hardening`.
Redis Streams remains the transport. Native Windows changes are excluded.

## Progress

- Implemented: concurrent Artifact writes and notebook appends.
- Implemented: Runtime drain intent and active Target configuration safety.
- Implemented: Redis response/connect deadlines and bounded shutdown.
- Implemented: fair traversal of durable queued work.
- Implemented: early input limits, safe database error logs, settings validation.
- Validation: Ruff lint/format and ty pass; base suite 585 passed / 4 skipped;
  combined PostgreSQL/Redis suite 61 passed. The final bounded-cycle traversal
  refinement also passed the dedicated work admission tests and ty.

## Artifact command ownership

Before writing any Runtime file, reserve the idempotency key in
`command_receipts` with `materialization_status=PENDING`. This is a committed,
durable reservation, not a Redis or process-local lock. Different content using
the same key is rejected before touching storage, including after a failed write.

PostgreSQL locks the Execution row while rechecking the receipt, writing files,
appending notebook Markdown, and recording the completed Artifact. Independent
Executor instances therefore serialize their writes for the same Execution.
Other Executions are not serialized. Lock acquisition has a 30-second deadline.

A successful transaction changes the receipt to `COMPLETED` with the Artifact
ID. A failed transaction keeps the reservation pending: retry the same request
with the same key. Notebook appends detect that key to avoid duplicate cells.
Completed retries return the existing ID without requiring the input file again.
Existing completed receipts without the new marker remain readable.

This is not a distributed transaction with the Runtime filesystem. An interrupted
request can leave a file before its DB completion; pending means unfinished,
not successful. There is no automatic retry daemon for these post-execution
commands. The caller retries the same command. This locking coordinates Executor
writes only; manual concurrent JupyterLab edits are not protected by the DB lock.

No public request/response or Redis event schema changes; no migration required.
PostgreSQL tests cover identical retries, conflicting concurrent requests,
independent notebook appends, and failure followed by retry.

## Runtime management

`DRAINING` persists through failed and recovered probes. Health failure is still
reported in the health error fields, but only explicit activation resumes new
assignment. Probe responses from an old endpoint, credential, or pool are ignored.

Endpoint/pool changes are rejected while durable reservations exist: active or
waiting Attempts, retry-retained sessions, and unresolved session cleanup.
Token rotation and capacity changes remain possible. A replacement server should
be registered as a new Target. Idle Target changes invalidate old observations
until the new endpoint is probed. Existing public schemas remain unchanged.

## Redis and admission

- `REDIS_CONNECT_TIMEOUT_SECONDS=5`: connection deadline.
- `REDIS_SOCKET_TIMEOUT_SECONDS=5`: response deadline; must exceed the Work
  consumer's 1-second blocking read. Service values override URL query timeouts.
- `OUTBOX_PUBLISH_TIMEOUT_SECONDS=5`: entire individual XADD deadline.
- `OUTBOX_SHUTDOWN_TIMEOUT_SECONDS=10`: graceful publisher join deadline,
  followed by cancellation and transaction cleanup.

Socket-driver automatic retries are disabled. Existing durable Outbox backoff
and consumer recovery continue to own retry behavior. A network failure ends
the current batch promptly instead of waiting once per selected DB row.
Publication remains at-least-once: timeout/stop after Redis acceptance can still
cause redelivery with the same event ID; consumer deduplication remains required.

Reconciliation reads 100 candidates per pass using `(created_at, id)` keyset
pagination, then wraps. Unschedulable early rows cannot permanently hide later
queued, finalizing or cancelled work. Each cycle captures its current tail so
continuous arrivals cannot postpone wrapping indefinitely. This adds one tail
lookup per complete cycle; each subsequent page still uses one bounded query.

## Input and logging safeguards

The existing per-operation Step limit is checked before resolving file inputs.
File readers consume at most their byte limit plus one byte before rejecting an
oversized file, including a file that grows between stat and read. Execution
output content and result-reference contracts are unchanged.

DB engines hide bound parameters. All configured logging handlers receive an
additional database exception filter; deployment formatters and other filters
are preserved. SQLAlchemy exception chains are replaced with error class and
SQLSTATE while traceback locations remain available. Driver DETAIL messages
are not logged because they can contain row values. Do not stringify DB errors
into unrelated log text before handing them to logging. This is not a general
scrubber for arbitrary user-provided log messages or structured extra fields.

Startup rejects heartbeat intervals greater than or equal to their lease.
The PostgreSQL test helpers preserve pytest logging configuration to remove the
previous order-dependent missing-payload warning assertion failure.

## Validation scope

- Full base suite includes the local TCP blackhole test, bounded Outbox
  publication and shutdown, input/log safety, and existing execution recovery.
- Real PostgreSQL tests use disposable, uniquely named databases. Redis tests
  use unique keys in the local test DB, not application Stream cleanup.
- Initial Redis integration server: **7.4.10**. A subsequent exact-version
  **6.0.8** run passed all 61 integration tests plus two service-client
  acceptance checks. See [Redis 6.0.8 validation](redis-6-0-8-validation.md).
  Functional acceptance does not replace the deployment's ACL/security review.
- Skipped: three opt-in live Docker download tests and one Linux UID permission
  test. No native Windows, Kubernetes/PV, or five-day soak execution was run.
- Operator deployment and existing service data were not reset or changed.
