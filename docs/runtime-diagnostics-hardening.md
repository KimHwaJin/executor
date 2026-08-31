# Runtime diagnostics hardening — 2026-08-31

## Status and scope

**Phases 1–5 delivered and tested; overall hardening is still open.**

Phase 2: [Runtime Output Completeness](runtime-output-completeness.md) fixes the
observed native Jupyter IOPub rate-suppression completeness mismatch. This document's
implementation details and test counts below describe Phase 1.

Phase 3: [Structured Runtime Diagnostics](runtime-structured-diagnostics.md)
adds bounded DB history and a cursor REST API through additive Alembic `0002`.
Existing result/event shapes stay unchanged. Phase 4 implements
[required-result completion](required-result-completion.md); remaining lifecycle
paths and MCP exposure remain follow-up work.

Phase 5 adds [background cleanup/probe diagnostics](background-runtime-diagnostics.md)
with stale-observation checks and bounded duplicate suppression. No new migration.

This phase repairs lost operational evidence without changing REST/MCP request or response
schemas, Redis event schemas, database columns, or Alembic revision `0001`.
It is not a production-readiness sign-off. The initial IOPub suppression failure is preserved as
historical evidence in [expanded output validation](output-expansion-validation.md); its supported
native-server detection path is now implemented in Phase 2.

## Implemented

| Boundary | Change |
| --- | --- |
| Jupyter output callback | Storage `PermissionError` / `TimeoutError` is not translated into a WebSocket transport error. |
| Jupyter channel close | Persist the observed received/sent close codes, or transport exception type. Do not copy peer close-reason text. |
| Shared output preparation / append / finalization | Preserve the original failure and seal any recoverable partial output. Record its descriptor on both Step and StepAttempt before completing their failure event. |
| Failure-result persistence | If sealing also fails, log it independently; do not replace the initiating failure with the secondary storage error. No descriptor is fabricated when no sealed result exists. |
| Timeout vs cancellation | The deadline owner seals timeout evidence with the actual timeout reason, rather than an earlier generic cancellation reason. |
| Notebook construction | Source/result read errors record projection `FAILED` and a reason instead of leaving it `PENDING`. |
| Notebook after failed code | Best-effort projection cannot replace the initiating code/timeout failure. Lease-loss checks remain authoritative. |
| Runtime abort / deletion / artifact registration | Emit bounded contextual failure logs, including returned abort-failure messages previously discarded. |
| Failure classification | A retained target outage is `RUNTIME_UNAVAILABLE` on Execution and Attempt. Code errors remain `TOOL_ERROR` even if the kernel cannot be retained. |

Generic transport/storage failures do **not** grant permission to reuse a possibly busy kernel.
The existing fenced cleanup and retry rules remain in effect. A failed MULTI Operation caused by
a recoverable Tool error can intentionally leave Execution `WAITING_FOR_OPERATION`; this does
not mean that the Operation succeeded.

## Operator log contract

New failure records contain JSON in the existing standard log message. No logging infrastructure
or formatter change is required. This is an **internal log record**, not a new Redis event.

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

- Search `execution_id`, then group by `attempt_id` and Step when present.
- Step result operations include Operation, Step, sequence and fencing generation.
- Runtime call records include target ID and, when available, sequence. Runner/cleanup records
  include session and Attempt IDs when available. Pre-Step operations need not have a Step ID.
- Context values are strings. Missing identifiers are omitted, not invented.
- `errors` retains up to 8 exception-chain entries and 16 stack locations per entry.
- Messages are bounded to 2,000 characters. Frames contain no source lines or local variables.
- Generic exception text is not copied: it may contain SQL, request parameters or user code.
  OS failures retain errno and its system description; controlled Runtime/storage messages are
  redacted. Credentials in URLs, common secret assignments and authentication schemes are removed.
- Arbitrary Tool exception messages and tracebacks belong in access-controlled Step result files;
  operational logs identify the code failure without copying those user-generated values.
- A failure can be observed at multiple layers, producing multiple records. These are observations
  of the same incident, not additional Execution failures. They occur on error paths, not per output.

Useful phases: `RESULT_PREPARE`, `RESULT_APPEND`, `RESULT_FINALIZE`, `RESULT_FAILURE_SAVE`,
`RUNTIME_EXECUTE`, `RUNTIME_TIMEOUT`, `EXECUTION_RUN`, `NOTEBOOK_BUILD`, `NOTEBOOK_WRITE`,
`NOTEBOOK_AFTER_FAILURE`, `RUNTIME_ABORT`, `RUNTIME_ABORT_RESULT`, `RUNTIME_DELETE_AFTER_ABORT`,
`RUNTIME_INTERRUPT`, `RUNTIME_DELETE`, `ARTIFACT_REGISTER`, `NOTEBOOK_ARTIFACT_REGISTER`.
Traced calls additionally record their existing `executor.runtime.*` operation name as phase.

## Where to look today

1. Execution detail: primary failure, retry/recovery state and notebook projection state/reason.
2. Operation/Step/Attempt detail: the affected scope and its failure history.
3. Step `result_ref`: sealed evidence, including partial results. Read the referenced manifest and
   its output files; `complete=false` does not mean no useful output was preserved.
4. Search `runtime.failure` logs by Execution ID for phase, exception chain and secondary failures.

Do not interpret absence of a Redis result reference as absence of evidence. The existing event
contract still omits incomplete references; REST can expose them when the descriptor was saved.
The result API is not yet a unified operational diagnostic endpoint.

## Validation

- `uv run ruff check .`: passed.
- `uv run ruff format --check .`: passed (352 Python files).
- `uv run ty check`: passed.
- `uv run pytest -q -m 'not postgres and not redis'`: **290 passed**, 28 deselected.
- `EXECUTOR_RUN_POSTGRES_TESTS=1 uv run pytest -q -m 'postgres or redis'`: **28 passed**
  (18 PostgreSQL, 10 Redis). PostgreSQL fixtures use disposable databases; no service data reset.
- New Worker failure-evidence matrix: **22 cases**, SINGLE and MULTI, real SQLAlchemy state and
  filesystem manifests with injected Runtime/storage failures. Checks include partial-file bytes,
  REST contract references, one completion event per Step, original failure preservation,
  cleanup failures, and notebook construction failures after an existing code/timeout failure.
- Additional log redaction/chain and Jupyter protocol regression tests exercise secret masking,
  callback exception identity, and channel close codes.
- Live local Jupyter gateway smoke: `basic` Python 3.11 with pandas/pyarrow, and `ml` Python 3.12
  with sklearn/xgboost/lightgbm, both passed REST/WebSocket execution; test kernels were deleted.

The live smoke loaded this branch's Python code. The running Executor Docker image was **not**
rebuilt or restarted; its image still needs updating before manual end-to-end verification of
this branch through the deployed REST/MCP service. Fault injection is not proof of every real
network, filesystem or platform failure mode.

## Next phases — not implemented by Phase 1

1. **Native Jupyter rate-limit detection — implemented in Phase 2.** Server-origin warnings now
   produce incomplete failure evidence without disabling limits or misclassifying ordinary
   kernel stderr. Other server variants and unknown remote losses still need separate validation.
2. **Durable structured diagnostics — DB/REST delivered in Phase 3.** Common
   code, phase, origin, severity, timestamps, scope and safe causes now preserve
   primary and secondary observations. MCP and event/manifest diagnostic
   references plus remaining lifecycle coverage are still open.
3. **Completion policy.** Explicitly define which projection/artifact steps are mandatory. Today
   bounded notebook-write failure remains a separate projection failure and need not fail code
   execution; Artifact registration and cleanup reasons are not all independently queryable.
4. **Remaining paths and resilience tests.** Driver construction, lease/recovery cleanup, process
   termination, DB-unavailable failure recording, and direct event access to partial evidence.
   If the DB or storage itself is unavailable, no universal durable-record guarantee is possible;
   sanitized operator logs and subsequent reconciliation are the fallback.

Track overall completion in [Production Readiness](production-readiness.md), PR-008.
