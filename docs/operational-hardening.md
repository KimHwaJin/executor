# Operational hardening review follow-up

Baseline: `2ea06b0` on `feature/redis-6-0-compatibility`.
Implementation branch: `feature/executor-operational-hardening`.
Redis Streams remains the transport. Native Windows changes are excluded.

## Progress

- Implemented: concurrent Artifact writes and notebook appends.
- In progress: Runtime drain intent and active Target configuration safety.
- Pending: Redis response/connect deadlines and bounded shutdown.
- Pending: fair traversal of durable queued work.
- Pending: early input limits, safe database error logs, settings validation.

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
