# Production readiness backlog

This document records confirmed hardening work that is required before production operation. These
items are not deferred product choices: each item remains open until its implementation and
adversarial regression tests satisfy the stated completion criteria.

## PR-001: Fence stale Worker ownership

- Priority: P0
- Status: IMPLEMENTED
- Area: Worker lease, Attempt ownership, MULTI resume, durable state transitions
- Public API impact: none
- Request impact: none; callers do not provide or receive the fencing value

### Problem

The former lease recorded an owner and expiry, but durable mutations did not prove that the calling
Worker still owned the current lease. A task whose lease expired could resume after recovery and
write a Runtime session, Step result, Artifact metadata, Outbox event, Operation result, or terminal
Execution state after ownership moved. An old heartbeat could also renew a later `RUNNING`
Execution because it was filtered by Execution status but not by the active ownership generation.

Heartbeat formerly started after workspace preparation and Runtime session creation. A slow
Runtime operation can therefore exceed the initial lease before heartbeat renewal begins even
though the Worker process is still alive.

### Implementation

- Alembic baseline revision `0001` stores a non-negative monotonic `fencing_token` on each Execution and
  ExecutionAttempt.
- Every claim and waiting MULTI resume increments the token while holding the Execution row lock;
  expired-lease recovery revokes the previous token before releasing ownership.
- The internal immutable `ExecutionLease` carries the Execution, Attempt, owner, and token through
  the Worker. It is not part of REST, MCP, or Redis contracts.
- Heartbeat begins immediately after claim and renews both rows in one transaction only when the
  owner, token, status, and unexpired deadline all match.
- Runtime-session recording, Step and StepAttempt transitions, Operation completion, Artifact and
  notebook registration, finalization, and their Outbox writes validate the same lease inside the
  mutation transaction. A mismatch raises `ExecutionLeaseLostError`; the stale Worker discards its
  result without cleanup or a terminal event.

### Required design

- Generate a new monotonically increasing lease generation whenever an Execution is claimed or a
  waiting MULTI Execution is resumed. Reusing an Attempt must not reuse its prior generation.
- Persist the active generation with the Execution and Attempt ownership records and carry it only
  in the internal Worker execution context.
- Start heartbeat immediately after a successful claim, before workspace preparation or Runtime
  session creation.
- Fence heartbeat, Runtime session recording, Step start/result, StepAttempt history, Operation
  completion, Artifact registration, notebook registration, terminal finalization, and their
  Outbox events with the expected owner, Attempt, and lease generation.
- Treat a zero-row guarded mutation as lost ownership. The stale task must stop without producing
  another terminal transition or Outbox event.
- Do not expose the lease generation as a required REST or MCP field. It is Executor-internal
  concurrency state; an optional read-only diagnostic field may be considered separately.
- Do not let a stale task delete a Runtime session now owned by another generation. Runtime cleanup
  remains the responsibility of the current recovery owner.
- Require abandoned Runtime session cleanup to complete before a `FROM_START` retry is admitted.

### Completion criteria

- A Worker delayed beyond lease expiry cannot update Execution, Attempt, Step, StepAttempt,
  Operation, Artifact, notebook metadata, or Outbox state after another generation takes ownership.
- A stale heartbeat cannot extend the lease of a later Attempt or MULTI Operation.
- Slow workspace or Runtime session creation cannot expire solely because heartbeat starts late.
- SINGLE retry and MULTI resume both issue a new generation and reject the previous generation.
- Multi-Worker PostgreSQL regression tests deliberately release a stale task after takeover and
  prove that every guarded mutation updates zero rows and produces no stale event.
- Real Runtime recovery tests prove that the abandoned session is cleaned before a replacement
  execution is admitted and that no Runtime reservation or session is leaked.

### Boundary

Lease fencing prevents stale ownership from corrupting Executor's durable state. It does not by
itself stop Python already executing inside a Runtime or undo files already written to Runtime
storage. Runtime interruption and timeout safety are tracked as a separate production-readiness
item.

## PR-002: Stop and verify Runtime work after a timeout

- Priority: P0
- Status: IMPLEMENTED
- Area: Step timeout, Operation timeout, Runtime interruption, retry safety
- Public API impact: none required
- Request impact: none; existing Step and Operation timeout fields remain authoritative

### Problem

The former timeout cancelled only the Executor coroutine waiting on the Runtime WebSocket. It did
not interrupt the Python code already executing in the Jupyter kernel. Executor could therefore
record a Step or Operation timeout while the code continued to mutate kernel state and Runtime
storage. The timeout was classified as a Runtime execution failure and could retain the same
session for `FROM_FAILED_STEP`, allowing a retry or MULTI correction to target a kernel that was
still busy or whose state continued to change.

### Implementation

- The generic Runtime Driver exposes `abort_session`, which returns a bounded
  `RuntimeAbortResult`; Worker code is not coupled to Jupyter REST details.
- Jupyter interruption is followed by bounded kernel-state polling. Only an explicit `idle`
  response produces `IDLE_CONFIRMED`; missing kernels and failed confirmation cannot be reused.
- Execution and Attempt rows persist `runtime_abort_status`. Baseline revision `0001` includes the durable
  fields and constraints, while versioned Outbox events record abort start and outcome.
- SINGLE and MULTI timeout paths persist `PENDING` before touching the Runtime. Idle-confirmed
  sessions may resume from the failed Step; every uncertain outcome forces session deletion and
  `FROM_START` or a non-retryable terminal result according to lifecycle policy.
- Failed deletion preserves the session reservation and cleanup `FAILED`, which blocks replacement
  retry. Lease recovery and cancellation resolve interrupted `PENDING` workflows.
- Unit tests cover idle confirmation, missing sessions, abort deadline, deletion failure, retry
  blocking, MULTI correction, and lease-expiry recovery. The real-Jupyter timeout smoke proves a
  delayed marker is never written and an interrupted infinite loop returns to a responsive kernel.

### Required design

- Treat a Step or Operation timeout as a Runtime abort workflow, not only as cancellation of the
  local WebSocket listener.
- Send the Runtime-specific interrupt command and wait for a bounded, positive confirmation that
  the session returned to an idle state.
- Add a generic Runtime Driver operation for abort-and-confirm semantics so future Runtime types
  are not forced into a Jupyter-specific Worker contract.
- Do not admit `FROM_FAILED_STEP`, a MULTI correction Operation, or finalization while Runtime
  interruption is pending or its outcome is unknown.
- When interruption and idle confirmation succeed, persist the timeout and explicit abort outcome
  before deciding whether same-session recovery is allowed. Partial filesystem and external side
  effects remain execution evidence and must not be treated as rolled back.
- When interruption fails, times out, the session disappears, or idle cannot be confirmed, delete
  the Runtime session and permit only `FROM_START` where retry policy allows it.
- If session deletion also fails, retain the Runtime reservation, mark cleanup `FAILED`, and block
  replacement admission until maintenance cleanup or operator action resolves the orphan.
- Make timeout, interrupt, idle-confirmation, deletion, and final retry strategy observable through
  bounded failure and cleanup fields and durable Outbox events without exposing credentials or
  code output.

### Completion criteria

- Real Jupyter tests run code that writes a delayed marker after the configured timeout and prove
  that the marker is never written after successful interruption.
- An infinite-loop timeout reaches a terminal bounded abort result without leaving a busy kernel.
- A Runtime that ignores interrupt is deleted, reports cleanup outcome accurately, and cannot be
  reused by `FROM_FAILED_STEP` or MULTI correction.
- No retry or correction is admitted while Runtime abort or cleanup is pending.
- Successful same-session recovery, if retained as policy, begins only after the original kernel is
  confirmed idle and records that prior side effects may be incomplete.
- Step, StepAttempt, Operation, Attempt, Execution, Artifact, and Outbox state agree for successful
  interrupt, forced deletion, deletion failure, cancellation during abort, and Worker shutdown.

### Boundary

Runtime interruption cannot transactionally roll back files, external calls, subprocesses, or
other side effects already produced by user code. Tools that perform external side effects still
need idempotent or compensating behavior. Lease fencing in PR-001 remains required to prevent an
old Worker from persisting state after timeout ownership changes.

## PR-003: Reconcile durable reservations with observed Runtime sessions

- Priority: P1
- Status: IMPLEMENTED
- Area: Runtime admission, orphan session detection, cleanup recovery, Runtime Target API
- Public API impact: additive response fields only
- Request impact: none

### Current reservation model

Executor does not maintain a separate reservation counter or Redis-based reservation. A
`RUNNING` or `WAITING` `ExecutionAttempt` assigned to a Runtime Target is the durable slot
reservation. A failed or queued Execution with an unexpired retained session for
`FROM_FAILED_STEP` also reserves a slot. Target selection locks the Runtime Target row and creates
or resumes the Attempt in the same PostgreSQL transaction, preventing concurrent Workers from
admitting beyond the configured capacity. Redis Streams remain wake-up channels only.

### Problem

The Runtime probe records the actual active session count, but scheduling currently admits work
from PostgreSQL reservation state alone. If Runtime session cleanup fails after the Attempt becomes
terminal, the orphan session may no longer be included in the DB reservation count. A later Worker
can therefore admit work up to the configured DB capacity while the Runtime already has additional
live sessions.

Using only a live Runtime count is also unsafe: two Workers can observe the same free slot, and a
claimed Execution consumes capacity before its Runtime session becomes visible. External Runtime
HTTP calls must not be placed inside the Target row-lock transaction.

### Implementation

- A shared admission module counts distinct durable reservations from active Attempts, retained
  retries, and unresolved cleanup sessions. Runtime Target views and Worker claims use the same
  query rather than maintaining a counter.
- Alembic baseline revision `0001` stores `session_count_observed_at` separately from health and resource
  timestamps. Successful probes replace the observation; failed probes preserve it and mark it
  stale through health state.
- The fresh baseline schema stores immutable source snapshot references, bounded Step summaries,
  and fenced shared-volume result references. It has no legacy full-output or normalized output
  tables.
- Fresh observations use `max(active_execution_count, active_session_count)` for admission.
  Stale or unavailable observations fall back to DB reservations without representing the last
  Runtime count as zero.
- Target selection locks the DB row, calculates persisted admission usage, and creates the Attempt
  in the same transaction. Runtime HTTP calls remain outside this transaction.
- Runtime Target REST and MCP responses expose DB reservation, observed session, effective usage,
  freshness, remaining capacity, and capacity-blocked state. A fresh excess Runtime count emits an
  operational warning without deleting or quarantining an unowned session.
- The Worker maintenance loop claims stale cleanup `PENDING`/`FAILED` rows with `SKIP LOCKED`,
  retries session deletion outside the transaction, and releases the reservation only after the
  confirmed result is committed.

### Required design

- Keep PostgreSQL Attempt state as the atomic admission reservation; do not add a mutable counter
  or derive capacity from Redis Stream entries.
- Count unresolved session cleanup (`PENDING` or `FAILED` with a retained session identifier) as a
  DB reservation until cleanup succeeds or an operator resolves it.
- Keep periodic Runtime probing as the source of the observed active-session count and persist only
  the latest observation and its timestamp so all Executor replicas share the same view.
- For a fresh observation, calculate admission usage as
  `max(active_execution_count, active_session_count)`.
- If the observation is stale or unavailable, use the durable DB reservation for admission and
  expose the observation as stale rather than representing it as zero.
- Block new admission when the effective usage reaches capacity. A transient mismatch does not
  immediately quarantine the Target; persistent mismatch or failed cleanup triggers maintenance
  cleanup and an operational warning. Quarantine may be added only if repeated cleanup cannot
  restore consistency.
- Do not call the Runtime status API synchronously while holding a PostgreSQL Target lock.

### Runtime Target response

Keep the two sources visible instead of overwriting one with the other:

- `active_execution_count`: PostgreSQL reservations owned by Executor.
- `active_session_count`: latest session count observed from the Runtime.
- `admission_used_count`: effective count used by scheduling.
- `available_capacity`: non-negative remaining configured capacity.
- `admission_blocked`: whether capacity currently rejects new work.
- `session_count_observed_at` and `session_count_fresh`: observation time and freshness.

Normal list and detail APIs return the recent persisted observation. The explicit probe command
refreshes it from the Runtime before returning.

### Completion criteria

- Concurrent Workers cannot exceed Target capacity while Runtime sessions are still being created.
- A cleanup-failed session continues to consume admission capacity immediately, including before
  the next Runtime probe.
- When the observed Runtime session count exceeds DB reservations, admission uses the observed
  count and does not create another session beyond capacity.
- Stale and failed probes never appear as a zero-session observation.
- Successful maintenance cleanup releases the unresolved reservation and restores available
  capacity without manually editing counters.
- Runtime Target REST and MCP responses expose DB reservation, observed session, effective usage,
  freshness, and availability consistently.

## PR-004: Preserve full Runtime output without unbounded Executor buffering

- Priority: P1
- Status: IN_PROGRESS
- Area: Runtime output collection, notebook projection, PostgreSQL result state, Agent access
- Public API impact: shared-volume result references on Step detail and consolidated result reads
- Request impact: none required for normal execution submission

### Agreed direction

- Do not solve capacity risk by silently discarding or permanently truncating the execution result.
- Preserve complete cell output on Agent/Executor shared storage and in the final notebook, subject only to
  an explicit platform-wide safety ceiling that is still to be measured.
- Keep Redis integration events bounded to output summaries and result references.
- Do not expose a general filesystem Tool to the LLM. Agent application code resolves a safe
  relative result reference, validates manifests and content checksums, and selects what enters the
  model context.
- Distinguish semantic results such as metrics and compact tables from large visual/data output and
  repetitive diagnostic logs.

### Implementation preparation

- `scripts/t35_output_measurement.py` implements the required text, image, and concurrency matrix,
  with a small default smoke preset and explicit confirmation for the resource-intensive full run.
- Each scenario records Executor container RSS, actively probed Runtime memory, PostgreSQL database
  and table growth, Runtime-owned notebook size, result API response bytes and latency, and Agent
  retrieval call count. It checkpoints the JSON report after every completed scenario.
- The harness never changes Runtime Target capacity. It fails when requested active concurrency
  exceeds configured capacity unless the operator explicitly chooses a queued-load measurement.
- The fencing, commit ordering, path safety, and Agent-read contract is recorded in
  [Shared execution result storage](shared-result-storage.md).
- The Worker creates one fenced partial result directory, commits each received output to native
  files, and atomically seals a terminal manifest on success, Tool error, timeout, or cancellation.
- PostgreSQL and Redis carry bounded summaries plus references only. There is no compatibility
  duplication of complete output bodies.
- `RUNTIME_MAX_OUTPUT_MESSAGE_BYTES` applies at the Runtime WebSocket receive boundary. The
  checked-in 32 MiB value is a conservative local default, not an approved production threshold.
- A limit breach records `OUTPUT_LIMIT_EXCEEDED`, seals already committed output with
  `complete=false`, and runs the same interrupt-and-confirm workflow as a timeout. Same-session
  retry is allowed only after positive idle confirmation.
- Step detail, Redis Step references, and shared manifests expose completeness and retained byte
  counts. Every representation remains native, checksummed, and untruncated.

### Problem

The production Jupyter WebSocket ceiling still needs deployment-specific measurement. Executor streams each
received output to shared storage without accumulating a complete Execution in memory or storing
full bodies in PostgreSQL and Redis. One individual WebSocket message can nevertheless be large,
especially for HTML or base64 image display data.

### Required design direction

- Store output incrementally in fenced shared-volume files instead of retaining an unbounded cell
  in Executor memory. The sealed manifest must be sufficient to project the complete notebook.
- Store bounded preview, output summary, byte size, media type, checksum, and stable result
  reference in PostgreSQL rather than duplicating every full payload in Step and StepAttempt rows.
- Keep a configurable per-message safety bound and measure it before production. Exceeding a hard
  bound must fail explicitly without claiming that discarded content is complete.
- Preserve clear metadata such as `complete`, `truncated_in_preview`, `size_bytes`, and
  `content_ref` so the Agent can decide whether additional retrieval is required.
- Avoid carrying full base64 images through Redis or ordinary JSON list responses.

### Approved image delivery

Cell-output images live as native files under the fenced shared Step result. Redis carries only the
manifest reference and bounded image summary. Agent application code validates the referenced PNG,
JPEG, SVG, or other file before giving the selected image to a multimodal model. Runtime-produced
plot artifacts remain on Jupyter-owned storage and use the separate Artifact contract.

### Measurement before thresholds

T35 must measure 1, 5, 10, 25, 50, and 100 MiB text output; 1, 10, 25, and 50 MiB image output;
and concurrency levels 1, 5, 10, and 20. Record Executor RSS, Runtime memory, PostgreSQL growth,
notebook size, result-reference API latency, and Agent retrieval calls. Final message, buffer, preview, and
storage ceilings are chosen from the deployed Pod limits and measured concurrency rather than
from an arbitrary universal constant.

### Completion criteria

- Large and repetitive output cannot grow Executor memory without a configured bound.
- Full retained output remains recoverable from shared result storage and the notebook after Executor
  restart.
- PostgreSQL and Redis retain bounded summaries and references rather than duplicate large image
  or text payloads.
- The Agent can discover output type and size, consume small results immediately, and retrieve
  every large output incrementally when required.
- Image retrieval works through the checksum-validated Agent/Executor shared-volume reference;
  Redis and ordinary APIs retain references only.
- T35 measurements and the selected production thresholds are recorded before this item is marked
  complete.

## PR-005: Controlled Executor maintenance and fail-safe restart recovery

- Priority: P1
- Status: IMPLEMENTED
- Area: Executor admission, maintenance, Worker shutdown, startup reconciliation
- Public API impact: additive administrative maintenance APIs
- Request impact: none for normal execution submission

### Approved scope

- Keep execution ownership in Executor. Do not introduce a Jupyter-side Runner or Collector in the
  current architecture.
- Persist an Executor-wide admission state in PostgreSQL so every Worker observes the same
  `ACTIVE` or `DRAINING` decision after restarts and when replicas are added.
- Provide administrative operations to drain new admission, inspect maintenance status, reactivate
  admission, and asynchronously terminate all active Executor-owned executions and Runtime
  sessions.
- A drain stops new claims while allowing already running executions to finish. Queued executions
  remain queued unless an explicit cancellation scope includes them.
- A terminate-running operation first stops admission, transitions active executions through the
  normal cancellation state machine, interrupts execution, and deletes only Runtime sessions and
  kernels whose ownership is recorded by Executor.
- Maintenance status exposes queued, active, cancel-requested, unresolved-cleanup, and active
  Executor-owned Runtime-session counts plus an explicit `safe_to_shutdown` decision.
- On unexpected Worker restart, reconcile non-terminal executions and remaining Runtime sessions.
  Do not blindly replay an operation whose completion is unknown. Clean up owned Runtime state and
  finalize it with an explicit `WORKER_LOST`-class failure unless an existing safe retry contract
  applies.
- Combine restart reconciliation with PR-001 fencing so a stale Worker cannot publish or persist a
  late result after recovery has taken ownership.

### Implemented foundation

- Alembic baseline revision `0001` seeds the singleton PostgreSQL `executor_maintenance` row in
  `ACTIVE`.
- `GET /api/v1/maintenance`, `POST /api/v1/maintenance/drain`, and
  `POST /api/v1/maintenance/activate` expose idempotent, audited global admission control.
- New Runtime admission share-locks the maintenance row in the Execution claim transaction. Drain
  therefore serializes against concurrent claims across replicas without rejecting submissions.
- Existing Runtime sessions, cancellation, cleanup, and local Worker shutdown remain independent
  of global admission.
- Maintenance status derives workload and owned Runtime-session counts from PostgreSQL and exposes
  `safe_to_shutdown`. See [Executor Maintenance](executor-maintenance.md).
- Alembic baseline revision `0001` persists `MaintenanceRun` and per-Execution targets. Run creation
  atomically drains admission and snapshots active work, while an expiring leased reconciler sends
  each target through the existing fenced cancellation state machine.
- Maintenance Run APIs provide asynchronous creation, summary lookup, and cursor-paginated target
  lookup. An expired Run lease is recoverable by another Worker without duplicating cancellation.

Worker startup now fences every expired or incomplete `RUNNING` lease in PostgreSQL before opening
Redis intake or queue reconciliation. The transition records `LEASE_EXPIRED`, increments the
Execution fence and recovery count, closes active Step/Operation/Attempt state, and persists the
terminal integration events transactionally. Runtime cleanup targets are reserved as `PENDING`
and processed asynchronously after the database startup barrier, so an unavailable Runtime cannot
make startup unbounded and its slot cannot be over-allocated. `/workerz` exposes the completed-at
time and recovered/cleanup-target counts for the current process start.

### Planned deployment procedure

1. Put Executor admission into `DRAINING`.
2. Wait for `safe_to_shutdown`, or explicitly start terminate-running maintenance.
3. Confirm that no active executions or unresolved Runtime sessions remain.
4. Deploy or restart Executor.
5. Re-enable admission and allow queued work to continue.

### Deferred architecture

A Runtime-side Runner or Collector that persists execution independently of Executor is not part of
the current scope. It may be reconsidered only if uninterrupted continuation across Executor Worker
failure becomes a hard requirement. Until then, an unexpected Worker loss is handled safely as a
failure rather than pretending that a disconnected execution can be resumed with complete output.

### Completion criteria

- Admission state survives Executor restart and is consistently enforced by all Workers.
- Planned maintenance can stop new claims without affecting running work.
- Operators can terminate all active Executor-owned work and verify Runtime cleanup without
  deleting unrelated sessions.
- Maintenance operations are idempotent, asynchronous, observable, and recoverable after restart.
- Unexpected Worker loss cannot cause an unknown operation to be silently replayed or accepted as
  successful.
- The documented deployment procedure produces `safe_to_shutdown` before a planned restart.

## PR-006A: Exclusive and recoverable cancellation ownership

- Priority: P1
- Status: IMPLEMENTED
- Area: Execution cancellation, reconciliation, Runtime cleanup, multi-Worker coordination
- Public API impact: none
- Request impact: none

### Problem

The former reconciliation loop scanned `CANCEL_REQUESTED` executions every two seconds and
dispatched their cancellation with replacement enabled. If Runtime interruption or session
deletion took longer than one reconciliation interval, the replacement cancelled the
already-running cancellation task and started another one. Multiple Worker replicas could also
attempt the same external Runtime cleanup because cancellation had no exclusive database-backed
owner.

### Implementation

- Alembic baseline revision `0001` includes cancellation-specific owner, expiry, and heartbeat
  fields plus a recovery index. Normal execution and cancellation ownership remain explicitly
  distinct.
- A cancellation claim locks the Execution row, establishes one expiring owner, increments the
  existing monotonic `fencing_token`, and clears the former execution lease. The prior execution
  Worker therefore cannot persist another Step, Artifact, Operation, terminal state, or Outbox
  event.
- An active execution lease is not preempted until its Worker has handled cancellation, preserved
  current-cell files as `INCOMPLETE` evidence, and explicitly released ownership. If that Worker
  disappears, cancellation waits for lease expiry instead. This establishes a deterministic
  evidence-preservation-to-cleanup handoff without weakening the fence.
- A cancellation heartbeat renews only the matching owner, fence, `CANCEL_REQUESTED` state, and
  unexpired lease. Another Worker can claim only after expiry and receives a newer fence.
- Runtime interruption/deletion starts only after a guarded lease check. Final Attempt, Step,
  Operation, Execution, cleanup state, and cancelled `execution.completed` Outbox writes occur in one
  transaction guarded by the same cancellation lease.
- Duplicate Redis delivery and two-second reconciliation do not replace a live local cancellation
  job. Other replicas may dispatch candidates, but PostgreSQL admits exactly one owner.
- Cancellation shutdown or an unexpected failure leaves the Execution in `CANCEL_REQUESTED`; after
  lease expiry another Worker safely resumes the idempotent Runtime cleanup. A cleanup failure is
  persisted as `FAILED` and continues to reserve Runtime capacity for the existing maintenance
  cleanup loop.

### Approved design

- Give Runtime cancellation and cleanup an exclusive PostgreSQL-backed owner and expiring lease.
- Acquiring cancellation ownership invalidates the prior execution fence. Every cancellation state
  write and Runtime cleanup completion is conditioned on the current owner and fencing token.
- Distinguish local execution jobs from cancellation jobs. Reconciliation never replaces a live
  cancellation job owned by the same Worker.
- Another Worker may claim cancellation only after its lease expires. Repeated Redis messages and
  reconciliation scans remain idempotent while the current lease is valid.
- Runtime interrupt and session deletion must be idempotent. A missing session is treated as an
  already-cleaned outcome when ownership metadata agrees.
- A cleanup failure is persisted and retains the unresolved Runtime reservation described by
  PR-003 instead of returning capacity optimistically.
- The external execution-cancel request and response contracts do not change.

### Completion criteria

- Runtime deletion taking longer than the reconciliation interval is not cancelled and restarted.
- Two Worker replicas cannot concurrently own cleanup for one Execution.
- Cancellation continues on another Worker after the previous owner's lease expires.
- A late execution Worker cannot finalize or publish output after cancellation takes ownership.
- Duplicate cancellation requests and Redis deliveries have no additional side effects.
- Cleanup failure remains observable and continues to affect Target admission until reconciled.

## PR-006B: Ordered and recoverable Execution integration events

- Priority: P1
- Status: IMPLEMENTED
- Area: Transactional Outbox, Redis integration events, Agent event consumption
- Public API impact: additive event ordering and recovery cursor
- Request impact: none

### Problem

Redis Streams preserve insertion order, but multiple Outbox Publishers can insert events for one
Execution in a different order from their committed domain order. Concurrent Agent consumers can
also finish processing adjacent events out of order. Redis Stream IDs and timestamps represent
publication, not authoritative per-Execution event order.

### Approved design

- Assign every Agent-facing integration event a monotonically increasing `event_sequence`
  scoped to its `execution_id` in the same PostgreSQL transaction that persists the event.
- Do not share this sequence with internal `executor.work` messages because Agent consumers cannot
  observe those messages and would see artificial gaps.
- Enforce uniqueness for `(execution_id, event_sequence)` and expose the sequence in the
  Outbox row, Redis envelope, durable event-list response, and recovery cursor.
- Keep `event_id` as the delivery deduplication key. Do not use timestamps, Redis Stream IDs, Step
  sequence, or Execution version as substitutes for aggregate event order.
- The Agent serializes event handling per Execution and durably stores its last contiguous
  processed sequence in Agent-owned state or its LangGraph checkpoint.

### Agent recovery rule

- `received == last + 1`: process the Redis event directly and advance the checkpoint.
- `received <= last`: ACK the duplicate or late event without applying it again.
- `received > last + 1`: do not apply the later event first. Query the PostgreSQL-backed Execution
  event-list API after `last`, process the missing contiguous range in order, advance the
  checkpoint, and then return to direct Redis processing.
- A delayed Redis copy of an event already recovered from PostgreSQL is deduplicated normally.
- Database recovery is scoped only to the affected gap and does not permanently switch that
  Execution or the whole Agent to database polling.

### Completion criteria

- Multiple Outbox Publishers cannot make an Agent regress or skip an Execution transition.
- A sequence gap is detected deterministically and repaired with one or more bounded event-list
  pages.
- Normal contiguous Redis delivery requires no event-list query.
- Late and duplicate delivery has no repeated Agent side effect.
- Agent restart preserves the last contiguous sequence and resumes without replay ambiguity.

## PR-006C: Bounded Redis Streams and decoupled durable event history

- Priority: P1
- Status: IMPLEMENTED
- Area: Redis retention, Transactional Outbox lifecycle, durable Execution events
- Public API impact: event history remains available independently of Outbox cleanup
- Request impact: none

### Problem

`XACK` removes a message from a consumer group's Pending Entries List but does not delete its Stream
entry. The primary Streams and DLQs therefore grow without bound. PostgreSQL Outbox rows also grow
without bound because the current implementation uses published Outbox payloads as the durable
Execution event-list source, coupling transport cleanup to public history retention.

### Approved design

- Configure and periodically enforce separate time-based retention for `executor.work`,
  `executor.events`, and their DLQs. Prefer approximate `XTRIM MINID` boundaries over one universal
  `MAXLEN`; final durations and hard ceilings are configurable and validated by load tests.
- Preserve internal work entries that are pending or not yet delivered within the supported work
  recovery window. PostgreSQL reconciliation remains the work source of truth.
- Permit Agent integration events to age out of Redis after the documented availability window.
  An Agent that is offline longer recovers its sequence gap from durable PostgreSQL history under
  PR-006B.
- Split durable Agent-facing `execution_events` from transport-oriented `outbox_events`.
  `execution_events` owns `event_id`, `execution_id`, `event_sequence`, type, bounded payload,
  occurrence time, actor, and trace context. Outbox rows own destination and publication/retry
  state and reference the durable event when publishing an integration event.
- Never automatically delete pending or unresolved failed Outbox rows. Delete published transport
  rows only after their configurable retention period.
- Retain durable Execution events longer than Redis and tie their final deletion to an explicit
  terminal-history retention policy or Execution hard delete.
- Give work, integration-event, and DLQ Streams independent retention settings and expose trim
  failures through logs and readiness/operational status rather than silently ignoring them.

### Initial policy to validate

- Internal work Stream: 1 to 3 days.
- Agent integration-event Stream: 7 days.
- Work DLQ: 30 days. The Agent-owned event DLQ is outside Executor retention ownership.
- Published Outbox transport rows: 7 days.
- Durable Execution event history: 90 days after terminal state or until Execution hard delete.

These are initial configurable values, not permanent product limits. Load, outage-recovery, and
operations requirements determine the production values.

### Implementation

- Agent-facing events are persisted in `execution_events` in the same transaction as their
  transport-oriented Outbox rows. The public event-history APIs read this durable table and only
  join the remaining Outbox row for optional delivery diagnostics.
- A PostgreSQL singleton lease permits only one Executor replica to run each retention pass.
- Every database deletion is bounded by a configurable batch size. Only old `PUBLISHED` Outbox
  rows are deleted; pending and failed delivery state is retained.
- Durable events are deleted only after the owning Execution is terminal, the terminal-history
  retention window has elapsed, and no Outbox row still references the event.
- The work Stream trim boundary never passes the age cutoff, any consumer group's last-delivered
  entry, or the earliest pending entry. A work Stream without a consumer group is not trimmed.
- The Agent event Stream and Executor-owned work DLQ are trimmed by their independent time windows.
  The event DLQ remains Agent-owned and is not modified by Executor.
- Unit tests, real Redis integration tests, and a two-manager PostgreSQL lease test cover the
  cleanup and recovery boundaries.

### Completion criteria

- Redis memory use cannot grow indefinitely from acknowledged Stream entries.
- Trimming never silently removes the only authoritative representation of work or an integration
  event.
- An Agent missing the Redis retention window can recover from durable event history by aggregate
  sequence.
- Published Outbox cleanup does not remove the event-list API history.
- Pending and failed deliveries remain recoverable and observable.
- Retention behavior is covered by real Redis integration tests, including pending messages,
  consumer downtime, trimming, and PostgreSQL gap recovery.

## PR-007: Mandatory and reproducible quality gates

- Priority: P1
- Status: READY_FOR_CI_WIRING
- Area: Formatting, lint, static typing, unit and integration tests, release evidence
- Public API impact: none
- Request impact: none

### Implemented baseline

- The repository uses Ruff formatting with a preferred 79-character line length. `E501` is
  excluded because Ruff intentionally preserves literals, URLs, and docstrings that cannot be
  split safely; all formatter-controlled structures still use the configured target.
- Runtime-storage test doubles implement the current Protocol instead of hiding missing methods
  with broad casts. Ruff, Ruff format, and ty all pass from a clean quality-gate invocation.
- `scripts/quality_gate.py` provides one platform-independent entry point for macOS, Linux, Windows,
  and internal CI.
- The default gate runs static checks and deterministic tests without depending on external
  services.
- The `--integration` gate requires real Redis and PostgreSQL, fails rather than silently skipping
  missing Redis, creates disposable PostgreSQL databases, applies Alembic to head, runs
  `alembic check`, and exercises multi-Worker concurrency.
- Real Jupyter lifecycle, failure, load, and soak checks remain a separate release gate through
  `scripts/local_validation_suite.py` so long or disruptive tests do not block every commit.

### Verified baseline

- Static/unit gate: Ruff passed, format passed, ty passed, 205 tests passed.
- Redis integration gate: 10 tests passed.
- PostgreSQL concurrency and migration gate: 16 tests passed.

### Remaining CI-platform work

The repository deliberately does not assume GitHub Actions because deployment uses an internal
CI/CD platform. That platform must invoke the documented commands and block merge or deployment on
failure. It must retain Runtime release evidence and inject all service credentials as secrets.

### Completion criteria

- Internal pull-request or build validation invokes the default quality gate and blocks on failure.
- A required integration job provisions Redis and PostgreSQL and invokes `--integration` without
  skipped required cases.
- Release candidates execute the real Jupyter validation level appropriate to the change and
  retain its generated summary and logs.
- Every PR-001 through PR-006C implementation adds its new concurrency, recovery, output, or
  retention cases to the appropriate required gate.
