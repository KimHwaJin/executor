# Structured Runtime diagnostics — 2026-08-31

## Scope

Phase 3 adds immutable, bounded diagnostic observations in PostgreSQL and a
cursor-paginated REST read API. It does not replace state/failure fields, change
SINGLE/MULTI semantics, emit new Redis events, modify result manifests, or change
the notebook/artifact mandatory-completion policy. MCP exposure remains deferred.

See [API contract](../dev_docs/execution-diagnostics.md) for all fields.
This document records Phase 3. The subsequent [Phase 4 completion policy](required-result-completion.md)
now rejects success on required post-code delivery failures and adds migration `0003`.

## Architecture and persistence

- Domain: Runtime-neutral code/phase/category/origin/severity/message/cause types.
- Application: independent diagnostic query port and immutable view.
- Infrastructure: safe exception mapper, fenced recorder, indexed SQLAlchemy
  reader and `execution_diagnostics` ORM model.
- Transport: shared Pydantic response models, REST route on Execution history.
- Alembic: additive `0002` following the actual 2026-08-31 `0001` baseline.
  Neither service DB nor Redis is reset by this work.

Each row references Execution with `ON DELETE CASCADE`. Attempt/Operation/Step IDs
are immutable scope snapshots validated by the recorder, not ownership links
that erase history when scope metadata changes. History is retained through retries
and for the lifetime of its Execution; no automatic diagnostic TTL is introduced.

The recorder locks and validates the active Execution/Attempt owner, fence and
lease deadline before insertion. Cancellation uses its separate cancellation
fence. A stale Worker cannot append a diagnostic to another owner's generation.
No transition, retry decision, event sequence allocation or Outbox publication is
performed by this recorder.

Diagnostics are independent error-path transactions. They are not atomically
committed with every state transition or with filesystem evidence. This permits
preservation of secondary observations without replacing primary failure fields,
but process death/DB outage can leave gaps. The absence of observations is not a
health assertion. Original logs remain the fallback.

## Recorded boundaries

| Boundary | Durable observation |
| --- | --- |
| Step prepare/append/finalize | Exact phase, safe cause, Step and Attempt IDs |
| Runtime execute/output limit/timeout | Execution or output category, specific limit code |
| Secondary result sealing | Separate `RESULT_FAILURE_SAVE`, retaining initiating failure |
| Notebook build and exhausted write retries | `NOTEBOOK`, separate from code success/failure |
| Artifact/notebook-artifact registration | `ARTIFACT` without overwriting earlier causes |
| Runtime abort and deletion after abort | Separate `CLEANUP` observations |
| Runner best-effort interrupt/delete | `CLEANUP` in SINGLE, MULTI and cancellation paths |
| Generic runner failure | `EXECUTION_RUN`; stored Step wrappers do not duplicate precise Step evidence |
| Cancellation target/driver setup | Missing target or driver creation failure |

Code exceptions never copy arbitrary user exception text into this new DB/API
surface. Their detailed traceback/output remains in the access-controlled result
files. Credential-bearing URLs, common secret assignments and authentication
values are redacted. Unknown exception text is not serialized. Cause chains are
limited to eight entries and explicitly flag truncation/cycles.

## Cost and failure isolation

No diagnostic DB call is added to successful Step output collection. Only errors
cause writes; even notebook write retries produce one durable exhausted-retry
observation rather than a new write on every intermediate retry. A repeated error
across distinct boundaries may produce multiple rows; this is observation history,
not a deduplicated Incident system.

Step observations are collected locally and persisted **outside** the execution
deadline. A slow diagnostic DB write must not turn an already observed code error
into a Step timeout. Each persistence attempt has a two-second async deadline.
Persistence failure logs the initiating error plus `DIAGNOSTIC_PERSIST` and does
not replace the exception; cancellation is not swallowed. Multiple observations
can add multiple bounded waits to an error path. No production throughput/latency
claim is made from correctness tests alone.

Ordinary result/list APIs and Redis payload sizes stay unchanged. Diagnosis is an
explicit paginated query (1–200 rows) using `(execution_id, created_at, id)` and an
Attempt-scoped index. Operation/Step filtering uses the Execution-scoped index;
additional indexes can be justified by later measurements. There is no total-count
scan or output/base64 body duplication.

## Validation and remaining boundaries

- Full regression: **347 passed**; PostgreSQL/Redis integration: **29 passed**.
- Ruff check/format and ty: passed.
- Live Jupyter basic/Python 3.11 and ML/Python 3.12: **12 cases passed**,
  SINGLE/MULTI × normal/ordinary warning echo/native 5 MiB data-rate breach.
- Example live limit cases: basic SINGLE
  `48e85290-f23b-4d1b-8228-6000dac7390e`, ML MULTI
  `59141b56-2071-4aa4-a147-c8f86e95f6c7`.
  Every limit case preserved `OUTPUT_DATA_RATE_LIMIT_EXCEEDED` with Step scope;
  successful controls produced zero diagnostic rows. These IDs belong to the
  isolated harness DB, not the running service DB. Test kernels were deleted and
  temporary DB/shared results discarded; notebooks remain on Jupyter storage.

Regression tests cover real Worker state/output evidence for SINGLE/MULTI,
primary plus secondary failures, native output-limit diagnostic codes, protected
cause chains, REST validation, filtered pagination, stale/expired/foreign owners,
cancellation fencing, DB persistence failure/deadline, and slow diagnostic I/O
not changing code errors into timeouts.

PostgreSQL tests use disposable databases and real Alembic migrations, including
`0001` → `0002` with an existing Execution, concurrent diagnostic writes, cursor
pagination, stale fences, cascading deletion and schema/model comparison.

The live Jupyter harness additionally checks durable diagnostics for native rate
limits and zero diagnostic rows for normal output and ordinary warning echoes.
It uses current Python code with isolated SQLite/shared temporary storage; it
does not rebuild the running Docker Executor or publish actual Redis events.

Still open: full structured coverage of lease-expiry recovery/retained-session
cleanup and process-shutdown paths, mandatory notebook/artifact completion policy,
MCP diagnostic Tool, cross-surface diagnostic references in events/manifests,
durable incident grouping and production-scale diagnostic retention measurements.
Arbitrary remote output loss without an observable signal cannot be diagnosed by
these fields alone. PR-008 is **not** fully complete.
