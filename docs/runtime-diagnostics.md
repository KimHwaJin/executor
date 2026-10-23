# Runtime diagnostics

How Executor records why an Execution, Step or cleanup went wrong, and where an operator or Agent
reads it. Field-level API details are in
[Execution diagnostics API](../dev_docs/execution-diagnostics.md). Output-completeness detection
is in [Runtime output completeness](runtime-output-completeness.md), and the mandatory delivery
policy is in [Required result completion](required-result-completion.md).

## Evidence layers

| Layer | Holds | Read through |
|---|---|---|
| Execution / Operation / Step / Attempt state | The primary failure (`failure_type`, status, retry and projection state) | REST detail, Redis events |
| Sealed result reference (`result_ref`) | Code, text and image output preserved on shared storage, including partial output | Step and Operation events, REST detail, manifest |
| `execution_diagnostics` table | Immutable secondary observations: cleanup, notebook, Artifact and storage failures | `GET /api/v1/executions/{id}/diagnostics` |
| `runtime.failure` operator log | Sanitised exception chains for every observed failure, including ones the DB could not store | Log search by `execution_id` |

Rules that hold across all layers:

- A later observation never overwrites the initiating failure. A cleanup `PERMISSION_DENIED` stays
  separate from the original `TOOL_ERROR`.
- Diagnostics never change Execution state, version, retry decisions or event sequence. A row with
  `severity=ERROR` is not a final status, and its count must not trigger a failure or a rerun.
- No false completeness: output known to be incomplete is reported with `complete=false`.
- The absence of rows is not a health assertion. Process death or a DB outage can leave gaps, and
  the operator log is then the fallback.

## Partial result references

Step and Operation completion events (`execution.step_completed.payload.result_ref`,
`execution.operation_completed.payload.step_results[].result_ref`) carry a required `complete`
boolean. `size_bytes` and the checksum describe the manifest file; output file sizes and
checksums are inside the manifest.

| Situation | Status and reference |
|---|---|
| Output collected, code succeeded | `SUCCEEDED`, `complete=true` |
| Code error fully received | `FAILED`, `complete=true` |
| Timeout, cancellation or output limit interrupted collection | `FAILED` or `CANCELLED`, `complete=false` |
| Sealing or DB attachment failed | `result_ref=null` |

`complete` is about the retained output, not about success. A referenced manifest is always
sealed and readable, even when `complete=false`. `output_summary` counts only retained output and
is null exactly when the reference is null.

When a Step is interrupted cooperatively, the deadline owner seals one terminal manifest from the
output already received, then attaches it to Step and StepAttempt after re-validating the lease
owner, fence and Attempt. That attachment has a two-second deadline, holds no DB lock during
remote or file I/O, and does not change state or event sequence; the normal cancellation path
records the terminal state afterwards. A Worker that lost its lease cannot attach a result to the
new generation. An attachment failure is recorded as `RESULT_REFERENCE_PERSIST`.

Consumers:

1. Join the relative path to the shared root safely and verify the manifest size and checksum.
2. Match Execution, Operation, Step, Attempt and `complete` between the reference and the manifest.
3. Verify each source and representation file the manifest lists before reading it.
4. Show `complete=false` as a partial result together with status and error.
5. On `null`, do not guess internal paths; use the diagnostics API or logs.

Never rerun code automatically because output is incomplete: its side effects may already have
happened.

## Active-Worker observations

`DiagnosticRecorder` writes rows for the Worker that currently owns the Execution. Before each
insert it locks the Execution and validates owner, fencing token and lease deadline (cancellation
uses its own fence), so a stale Worker cannot append to another generation.

| Boundary | Phases |
|---|---|
| Step result files | `RESULT_PREPARE`, `RESULT_APPEND`, `RESULT_FINALIZE`, `RESULT_FAILURE_SAVE` |
| Runtime execution | `RUNTIME_EXECUTE`, `RUNTIME_TIMEOUT`, output-limit codes |
| Notebook projection | `NOTEBOOK_BUILD`, `NOTEBOOK_WRITE` (once, after retries are exhausted), `NOTEBOOK_AFTER_FAILURE` |
| Artifacts | `ARTIFACT_REGISTER`, `NOTEBOOK_ARTIFACT_REGISTER` |
| Runtime cleanup | `RUNTIME_ABORT`, `RUNTIME_ABORT_RESULT`, `RUNTIME_DELETE_AFTER_ABORT`, `RUNTIME_INTERRUPT`, `RUNTIME_DELETE` |
| Runner | `EXECUTION_RUN`, cancellation target and driver setup |

Successful output collection makes no diagnostic writes. Step observations are collected locally
and persisted outside the execution deadline, each with a two-second deadline, so a slow DB write
cannot turn a code error into a timeout. A failed write logs `DIAGNOSTIC_PERSIST` and keeps the
original exception.

## Background observations

Lease recovery, retained-session cleanup and the MULTI waiting-session audit run without an
Execution lease. `BackgroundDiagnosticRecorder` captures a `RuntimeObservation` (status, version,
fencing token, Operation, Target, session) when the pass reads a row, and compares it with the
current row before storing a diagnostic, applying a cleanup result or failing an expired wait. A
changed row means the observation is stale and is dropped.

| Phases | Meaning |
|---|---|
| `RECOVERY_VALIDATE`, `RECOVERY_TARGET` | Cleanup target identity or Runtime Target missing |
| `RECOVERY_DRIVER_CREATE`, `RECOVERY_DRIVER_CLOSE` | Runtime client could not be created or closed |
| `RECOVERY_SESSION_DELETE` | Leftover kernel or session could not be deleted |
| `RECOVERY_RESULT_PERSIST` | Deletion result could not be written; the PENDING reservation stays for the next pass |
| `MULTI_DRIVER_CREATE`, `MULTI_SESSION_PROBE`, `MULTI_DRIVER_CLOSE`, `MULTI_AUDIT` | Waiting-session probe failures |
| `LEASE_RECOVERY_SCAN`, `RETAINED_CLEANUP_SCAN`, `MULTI_LIFECYCLE_SCAN` | The scan query itself failed; logged only, because no Execution can be identified |

Interpretation:

- A transient MULTI connection failure keeps `WAITING_FOR_OPERATION` and its deadline. Only a
  definitive "session does not exist" answer applies the `RUNTIME_SESSION_LOST` rule.
- If the kernel was deleted and only `close()` failed, cleanup can still be `SUCCEEDED`.
- Retained-session expiry first reserves the row as NOT_RETRYABLE/PENDING under a row lock, then
  deletes the kernel without holding the lock, so a retry cannot race onto that kernel. Remote
  deletion is not a distributed transaction and may be repeated; it relies on idempotent deletes.

Duplicate suppression: the same Execution generation, Operation, phase and sanitised cause is
stored at most once per 300 seconds. A per-process cache (1,024 keys) skips repeats locally, and a
row-locked check of the last 128 rows of that phase suppresses them across Pods. Rows carry no
suppressed count or incident id.

## Operator log contract

`runtime.failure` records are JSON inside the standard log message, not Redis events:

```json
{
  "event": "runtime.failure",
  "occurred_at": "2026-08-31T00:00:00+00:00",
  "phase": "RESULT_APPEND",
  "execution_id": "example-execution-id",
  "operation_id": "example-operation-id",
  "step_id": "example-step-id",
  "sequence": "0",
  "attempt_id": "example-attempt-id",
  "fencing_token": "1",
  "errors": [
    {
      "type": "PermissionError",
      "message": "PermissionError: errno=13 Permission denied",
      "stack": [{"file": ".../io.py", "function": "atomic_write", "line": 42}]
    }
  ]
}
```

- Context values are strings, and missing identifiers are omitted rather than invented.
- Up to 8 exception-chain entries and 16 stack locations each; messages are capped at 2,000
  characters, and frames carry no source lines or locals.
- Generic exception text is not copied, since it may contain SQL, parameters or user code. OS
  errors keep errno and its description. Credentials in URLs, secret assignments and
  authentication schemes are removed.
- User tracebacks belong in the access-controlled Step result files, not in logs or diagnostics.
- One incident can be observed at several layers and produce several records.

## Where to look

1. Execution detail: primary failure, retry and recovery state, notebook projection state.
2. Operation, Step and Attempt detail: affected scope and failure history.
3. Step `result_ref`: sealed evidence, including partial output.
4. `GET /api/v1/executions/{id}/diagnostics`: secondary observations, paginated by
   `(execution_id, created_at, id)`, 1 to 200 rows per page.
5. `runtime.failure` logs by `execution_id`.

Open coverage work is tracked in [Production readiness](production-readiness.md), PR-008.
