# Live notebook Artifact availability

## Contract

- First successful notebook preparation registers an Execution-scoped
  `NOTEBOOK` / `AVAILABLE` file before any Step need have run.
- Notebook projection refreshes the same row's observed file metadata and
  updated audit fields. Created fields and Artifact ID remain stable across
  Steps, MULTI Operations, failures, retained retries and finalization.
- New managed notebook entries have null Attempt/Step producer fields. They
  describe the current saved Execution file, not a historical output snapshot.
- AVAILABLE means saved-file availability, not successful execution, complete
  cell output or a promise that the Runtime/PV is currently reachable.
- Existing success gates still require successful Steps, complete immutable
  result evidence and a successful notebook projection. Registering the initial
  file cannot make a failed/incomplete execution successful.
- Failure/cancellation does not remove the last successfully registered file.
  A failed replacement can leave the previous saved version downloadable;
  notebook projection status/diagnostics communicate the delivery failure.
- The first registration is serialized under the existing Execution lease
  lock. A cancellation request arriving immediately after save is allowed only
  while the same lease fence is still owned; stale owners remain rejected.
- No request/response structural change, event schema change, migration or
  Jupyter extension/image update is required. Notebook metadata includes
  `content_semantics=CURRENT_FILE`. New producer scope and availability timing
  are intentional behavior changes.
- No historical file scan/backfill or deletion is performed. Already registered
  paths reuse their existing earliest Artifact ID when next saved. Existing
  duplicate historical rows are not deleted by this change.

## Downloads and concurrency

`GET /api/v1/artifacts/{artifact_id}/content` is unchanged. Without Range it
reads the entire file opened by the Runtime, using that descriptor for size,
hash and streaming, not the DB's observed size/checksum. Each full-file download
hashes then reads the file; many concurrent requests still consume storage I/O.

Executor saves use same-directory temporary files and atomic replacement.
POSIX readers already holding the previous file continue reading that version;
new opens see the replacement. Downloads do not lock or stop code execution.
External in-place writes are only detected best-effort and may abort streaming;
this is not an arbitrary-writer snapshot/lock service. BFFs must stream bytes,
preserve transfer failures and avoid presenting an interrupted file as success.
Real production PV filesystem semantics still require deployment verification.

## Verification (2026-09-17)

Environment: isolated PostgreSQL 17, Redis 6.0.8 and existing local Jupyter image
with the current extension. Existing user DBs, Streams and Compose services were
not reset. Disposable database/Stream names and containers were cleaned up.
Docker overlay had no free space; temporary DB/Redis data used tmpfs and Jupyter
used a separate host-mounted test workspace.

- New unit scenarios: initial file before Step Attempt, same-ID metadata
  refresh, lease fencing, cancel-after-save registration, SINGLE/MULTI running,
  failure, timeout, retry/correction, finalization and cancellation; missing
  initial file and failed replacement. **11 passed**.
- Real PostgreSQL suite: **46 passed**, including eight concurrent notebook
  registrations returning one ID; Alembic upgrade/check passed.
- Real REST/Jupyter smoke: SINGLE success, SINGLE failure/download/retry,
  SINGLE cancel/download, MULTI waiting/download/finalize. **291 downloads**
  checked notebook JSON validity, Content-Length, ETag and SHA-256 while four
  concurrent readers spanned notebook replacements. Same Artifact ID retained.
- Existing SINGLE/MULTI MCP/REST lifecycle E2Es passed, including cancellation,
  retained retry, error correction and DB/Outbox/Redis event agreement.
- Existing download primitive tests: **32 passed**, covering atomic replacement,
  in-place mutation detection, file size changes, Range and read cleanup.
- Full pytest: **798 passed / 92 environment-dependent skipped**. The separately
  enabled PostgreSQL and Redis suites above cover their relevant skipped cases.
  Ruff, ty and git whitespace checks passed.

Reproduce with disposable local dependencies configured for the existing
`redis_compatibility_lifecycle_smoke.py` wrapper:

```bash
PYTHONPATH=scripts uv run python -c 'import asyncio; from redis_compatibility_lifecycle_smoke import main; asyncio.run(main(smoke_scripts=("live_notebook_artifact_e2e.py", "single_failure_retry_cancel_e2e.py", "multi_execution_lifecycle_e2e.py")))'
```

The wrapper requires `DATABASE_URL`, `EXECUTOR_REDIS_TEST_URL`,
`REDIS_COMPAT_JUPYTER_ENDPOINT` and `REDIS_COMPAT_JUPYTER_TOKEN`; it creates its
own database/streams and Executor process. It does not create or reset dependency
servers. Set `REDIS_COMPAT_RUNTIME_PROFILE` when not using `default`.

The newly added smoke initially sent an unsupported `expected_version` to
cancel/retry. That test-request error was corrected; those endpoints remain
unchanged. The corrected full smoke suite passed.
