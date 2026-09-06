# Runtime Target purge: implementation and validation

Date: 2026-09-06.
Branch: `feature/runtime-target-purge-history`.
Contract: [POST Runtime Target purge](../dev_docs/post-runtime-target-purge.md).

## Delivered

- Preserve the REST POST route, reduce its body to required `actor`.
- Deduplicate by Target UUID and immutable deletion record, not a caller key.
- Remove only disabled registrations after durable work and cleanup checks.
- Preserve Execution/Attempt UUIDs, all subordinate history and Artifact rows.
- Same-name/endpoint registration gets a new UUID using a new registration key.
  Replaying the old registration key returns a clear conflict; replaying the old
  purge returns its original audit record without touching the new registration.
- Keep same-type/same-pool storage fallback; do not migrate or remove any files.
- No MCP Tool or Redis event change. No Execution deletion implementation.

## Migration

`0005` removes two active-registry foreign keys without nulling their values and
removes purge-specific idempotency/fingerprint columns. PostgreSQL migration tests
compare business rows before/after upgrade, retain an existing deletion record,
exercise a schema-only downgrade/upgrade, and reject rollback once historical
references point to deleted registrations. Alembic's metadata comparison passes.
The new schema requires coordinated replacement of old Executor processes.

No application database was reset or migrated during this task. PostgreSQL tests
create uniquely named disposable databases and remove only those databases.
Existing Compose services, registered servers, Redis Streams and PV files were
not changed. Temporary Jupyter containers and their temporary mounts were removed
by the test harness after verification.

## Verification results

```bash
uv run python scripts/quality_gate.py --integration
```

- Ruff lint and format: passed.
- ty: passed.
- Base suite: **666 passed, 6 skipped** (77.84 seconds).
- Redis suite: **27 passed**, local default Redis 6.0.8.
- PostgreSQL suite: **44 passed** (25.65 seconds), including migrations.

The six base skips are five explicitly opt-in live Jupyter download cases and
one Linux UID-switch permission case. Three of the five live cases were then
verified separately with:

```bash
RUN_ARTIFACT_DOWNLOAD_LIVE=1 uv run pytest -q \
  tests/test_artifact_download_live.py \
  -k 'separate_pool_pvs or large_binary' --tb=short
```

Result: **3 passed, 2 deselected** (10.27 seconds).

## Scenarios covered

- Required actor, removed fields rejected, unknown UUID 404, disabled prerequisite.
- Repeated deletion retains the first actor/time, including a different later actor.
- Eight concurrent PostgreSQL requests commit exactly one deletion record.
- Real PostgreSQL lock waits against deletion, activation and reservation creation;
  losing operations reject safely without deleting an in-use registration.
- Active/waiting/finalizing/cancelling work, retained and expired-but-uncleaned
  kernels, failed/pending cleanup, and older Attempt cleanup block deletion.
- A later Attempt cleaning the same kernel permits deletion; cleaning a different
  kernel is not proof. The latest Execution cleanup result is also respected.
- Same-name re-registration and old UUID replay preserve the new registration.
- Successful deletion preserves Execution, Attempt and Artifact identity and
  removes the active record containing its credential; no Runtime deletion call.
- Real Jupyter roots contain different files at identical relative paths. After
  the original BATCH Target is disabled and purged through REST, Execution,
  Operation, Step, event, Attempt and Artifact reads still succeed. The historical
  Attempt response retains the old Target UUID.
- Downloads (full and Range), notebook reads, report writes and Markdown appends
  use the remaining BATCH server; INTERACTIVE files are unchanged. With BATCH
  unavailable, HTTP fails rather than serving the INTERACTIVE file.
- Large binary, empty file and atomic replacement download regressions pass.

## Boundaries

No real Kubernetes rollout, multi-day soak or production ACL verification was
performed. The live storage scenarios seed completed history and files; they are
not a new real-code-execution or LLM/Agent UI E2E run. Purge does not terminate a
Pod, kernel or an in-flight download. Same-pool shared storage remains an operator
deployment contract. Manual DB changes bypassing the registry are unsupported:
historical Target IDs now resolve logically to active rows or deletion records.
