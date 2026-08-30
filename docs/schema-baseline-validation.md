# Current schema baseline validation — 2026-08-31

## Outcome

The local Executor database was recreated from the current schema as revision `0001`.
Fresh migration, ORM schema comparison, container startup, real Jupyter SINGLE/MULTI
execution, and post-restart result retrieval all passed. Public API/event contracts are unchanged.

## Reset scope and recovery

- Recreated only the local Compose PostgreSQL database `executor`.
- Before reset it contained 24 Executions, including 3 stale non-terminal rows, and was stamped
  with discarded revision `0004`. Executor and Jupyter were stopped before reset.
- Cleared Redis logical DB 0 and DB 14 after confirming they contained only Executor main/test
  Streams. Stream deletion also removes their consumer groups and pending-entry state; the
  service recreates its work group on startup.
- PostgreSQL `executor_e2e_validation_0831a` and other databases were not reset.
- A PostgreSQL custom-format dump and Redis RDB snapshot were saved in a local temporary
  backup directory before deletion. The local handoff identifies its path. These backups are
  not committed and must be moved elsewhere if long-term retention is required.
- Shared result files and the Jupyter workspace were preserved. Old files are not reindexed
  into the recreated database. Test result files are generated in the existing separate volumes.
- Verified empty PostgreSQL public schema and empty Redis keyspace before migration/startup.
- Verified zero Runtime Targets and zero Executions after migration, before explicit test
  registration. No automatic default Jupyter registration was introduced.

This is a development reset, not an in-place migration for a deployed database. All databases
created before this reset require recreation, including earlier databases stamped `0001`.

## Repository changes

- Kept the full explicit schema snapshot as the only revision, `0001`, dated 2026-08-31.
- Removed compatibility bridge `0002` and its legacy-shape upgrade test.
- Aligned application readiness and migration/deployment guides with the new baseline.
- Added a migration graph/readiness consistency test.
- Replaced the legacy bridge test with repeated-upgrade preservation of event/Outbox IDs
  and singleton rows; added full downgrade/recreate coverage against real PostgreSQL.

## Verification

| Check | Result |
| --- | --- |
| Ruff lint / format / ty | Passed |
| Unit tests | 251 passed |
| Real Redis integration | 10 passed |
| Real PostgreSQL / migrations / multiple Workers | 18 passed |
| Empty DB upgrade | `0001 (head)` |
| Alembic schema check | No new upgrade operations detected |
| Migration repeat / downgrade and recreate | Passed on disposable PostgreSQL databases |
| Compose PostgreSQL / Redis / Executor / Jupyter | Healthy |
| Empty-fleet startup followed by explicit MCP runtime registration | Passed |
| SINGLE normal execution via REST and MCP | Passed; 8 public events per execution |
| SINGLE failure then retry through MCP | Passed; prior successful Step reused |
| SINGLE running cancellation through REST | Passed |
| MULTI REST/MCP follow-up Operations and correction | Passed; 4 Operations including an intentional failed Step |
| MULTI finalize and running cancellation | Passed |
| 1 MiB text / 1 MiB PNG output smoke | Both passed |
| Executor restart with completed work | Ready; stored results still accessible |
| Shared result vs. Runtime-owned notebook after restart | Code matches; text/PNG bytes identical; manifest checksums verified |

Executor image `executor-service:local` was rebuilt from the checked-in Dockerfile. Its image ID
was `sha256:c0e17255d9b5cf6ef5604a7a09988f9f9a97a867cdafeb60fd4a8be1d1b768c6` for this run.
The existing `executor-jupyter:local` image was reused without changing Jupyter code or dependencies.
External base-image metadata lookup delayed the Executor build, but the build completed normally.

## Output smoke measurements

Each scenario used one active execution and generated 1,048,576 bytes of output. RSS was sampled;
these observations are not a proof of worst-case peak memory or an approved production limit.

| Output | Sampled Executor RSS increase | Result API bytes | Result API latency | Notebook bytes |
| --- | ---: | ---: | ---: | ---: |
| Text, 1 MiB | 192,512 | 2,601 | 15.805 ms | 1,049,383 |
| PNG, 1 MiB | 2,678,784 | 2,598 | 10.741 ms | 1,399,588 |

Each result API call returned references rather than copying the large output body. Separate
post-restart verification loaded the referenced files through the checksum-validating reader
and compared full bytes with the Jupyter notebook read API. PNG base64 in the notebook accounts
for its larger serialized size. No content truncation was observed in these two scenarios.

## Local evidence

Logs and generated JSON reports are under `test-results/schema-baseline-20260831/` (gitignored):

- `quality-gate.log`
- `preflight.log` and `local-test-preflight-578d259f3f2e44c689c60830763442f0.json`
- `single-observability.log`
- `single-lifecycle.log`
- `multi-lifecycle.log`
- `output-smoke.log` and `t35-output-measurement-345e69ee16df400ab497b90f7d6e9cdf.json`
- `restart-result-integrity.log`
- `final-state.log`

Final state after Executor restart: 8 Executions (6 succeeded, 2 intentionally cancelled),
72 public events with matching PostgreSQL/Redis IDs and contiguous per-Execution sequences,
zero unpublished Outbox rows, zero active Executions, and zero Jupyter sessions. The local
Executor, PostgreSQL, Redis, and Jupyter containers were left healthy and running for inspection.
The database is therefore no longer empty: it now contains only the newly registered test
Runtime Target and this run's execution evidence, not the pre-reset application data.

Representative executions:

| Scenario | Execution ID |
| --- | --- |
| SINGLE REST | `3bf03ee7-b3b2-4179-8669-08b33d845d9b` |
| SINGLE MCP | `dbd82760-232f-49bd-87a7-e3247e4e25df` |
| SINGLE retry | `13377160-8631-4d8c-8d5b-74488ffe8d87` |
| SINGLE cancel | `db8696f1-84b2-4d20-ae5a-701e7572d936` |
| MULTI correction / finalize | `a81095ba-e8e1-4e24-8ac9-3f1de4891449` |
| MULTI cancel | `2eb35eb4-bdcd-4a66-97aa-706d51530a6c` |
| Text output | `65f86258-ce53-458f-94da-8f521fe38b87` |
| PNG output | `13466935-558d-4f8b-983a-a4dcf112f6af` |

## Limits and next validation

This validates the reset baseline and functional regression, not production capacity or days-long
execution. The full T35 size/concurrency matrix, deployment-specific output limits, large-scale
soak testing, and internal CI wiring remain separate work. Existing deferred product decisions
were not implemented. No Kubernetes resources or remote databases were modified.
