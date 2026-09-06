# Runtime storage pool isolation

Date: 2026-09-06. Branch: `feature/runtime-storage-pool-isolation`.
Base: `81c66ca`.

## Contract

Runtime storage candidates are restricted to the Execution's persisted Runtime
type **and pool**. The preferred execution Target is used only when it belongs
to that scope and remains enabled and ACTIVE/DRAINING. Other eligible Targets
in the same pool follow by name. Missing, disabled, moved or failed preferred
Targets never permit a cross-pool fallback.

Applied to Artifact downloads, notebook queries, materialized text/report
writes, and notebook Markdown append reads/writes. Runtime execution itself
continues using its existing scheduler and assigned Driver.

Each pool must share a PV and root-relative layout internally. INTERACTIVE and
BATCH may use different PVs. This is an operator-owned topology contract; no
Kubernetes or filesystem identity probing is introduced. If a pool has no
accessible storage endpoint, the request fails rather than using another pool.
Execution slot availability does not restrict storage access.

Public REST/MCP requests, responses, Redis envelopes and database schema are
unchanged. The internal storage Protocol now requires a keyword-only
`runtime_pool`; all application callers pass the stored Execution value. It
is not inferred from the preferred Target's mutable registration.

Range and streaming rules are unchanged: file/range failures do not fall back,
and no Target switch occurs once download metadata has been handed downstream.

## Verification

- Complete quality gate: Ruff, formatting and ty passed; base regression
  **650 passed / 4 skipped**; Redis **27 passed**; PostgreSQL **34 passed**.
- Subsequently added opt-in real-Jupyter isolation regression and refactored
  its existing disposable-container fixture for reuse. Final lint/format/type
  checks passed after that addition.
- Live download checks: **2 passed** (separate-pool isolation and large binary,
  empty-file, range and atomic replacement regression).
- SQLite-backed isolation matrix checks both pools and all four storage
  operations: failing, missing, other-pool, absent and OFFLINE preferred
  Targets; DRAINING fallback; no same-pool candidates; all same-pool failures.
- Application tests verify stored-pool forwarding by download/notebook queries
  and report materialization including notebook append.

The new opt-in test uses two real Jupyter containers with separate temporary
root mounts and different content at identical paths. An unreachable preferred
batch endpoint falls back to the healthy batch endpoint, never the
alphabetically earlier interactive endpoint. Downloads and notebook reads
return batch content; report and notebook writes change only batch files.
After disabling the remaining batch endpoint, a real HTTP request fails rather
than returning the interactive file. Temporary containers and directories are
removed by their fixture; existing Compose services and PVs are untouched by
this isolated test.

Run the live regressions with:

```bash
RUN_ARTIFACT_DOWNLOAD_LIVE=1 uv run pytest -q \
  tests/test_artifact_download_live.py \
  -k 'separate_pool_pvs or large_binary'
```

## Local deployment and E2E

Only the Executor Compose image/container was rebuilt/recreated. The deployed
image ID is
`sha256:692da8b8616a97eeb9d461c0c7368ddd450149e8f6def7385a67432395802d40`.
Redis 6.0.8, PostgreSQL, the four Jupyter containers and existing storage were
not reset or recreated for this deployment.

Supplementary real executions all passed:

| Profile | Mode | Execution ID |
| --- | --- | --- |
| default | SINGLE | `2c8fafba-8024-40f2-b860-ab8a5e5aff08` |
| default | MULTI | `aa899107-b1f5-4d7b-98bf-6da78b6f26ea` |
| 3102311 | SINGLE | `399bc0a5-8dd9-4fcb-9bd0-2a5445255ef8` |
| 3102311 | MULTI | `809c2ad3-b425-437d-a1aa-67fe18420453` |

These cover 12 Steps, 44 events, stored-output verification, images/HTML/text,
full Artifact and notebook downloads, and same-kernel MULTI continuation and
finalization. They use the local INTERACTIVE pool; the separate-PV live test
above establishes the batch storage boundary. No new failure-recovery, load,
multi-day soak or production Kubernetes validation is claimed.

Execution deletion remains deferred (DD-008); Runtime registration deletion
behavior is not changed by this work.
