# Redis 6.0.8 compatibility

## Scope and rollback

- Baseline before this work: `318fb98` on `main`.
- Feature branch: `feature/redis-6-0-compatibility`.
- Server minimum for this branch: Redis 6.0.8. Python client: redis-py 5.3.x,
  frozen at 5.3.1 in both Executor and test Agent lockfiles.
- No PostgreSQL migration, API field, public event envelope, result path, or
  SINGLE/MULTI lifecycle change. Existing Streams and groups can be reused.
- This is application compatibility, not a claim that Redis 6.0.8 is a secure
  or currently maintained server release. The platform owns patching decisions.
- Rebuild the Executor image or run `uv sync --frozen`; rebuild/sync the test Agent
  independently. Do not reset Redis or downgrade the existing 7.4 AOF/RDB volume.
- Reverting code to the baseline also restores its Redis 7.2+ client support
  requirement; it will not work correctly on Redis 6.0.8.

## Recovery

`infrastructure/redis_streams.py` is the common primitive used by the Worker and
the reference Agent consumer. All server versions use this path, with no
exception-driven legacy fallback.

1. Scan one bounded `XPENDING` page, using inclusive ID bounds supported by 6.0.
2. Select entries that satisfy the idle timeout.
3. Check missing payloads, remove their PEL references with `XACK`, and report
   the missing count/ID sample in warning logs. This does not recover missing
   payloads: Executor relies on PostgreSQL reconciliation, and Agents on durable
   history reconciliation.
4. Claim existing payloads with `XCLAIM`, retaining the idle condition.
5. Process through the unchanged validation/dispatch/ACK path.

Steps 1–4 run atomically in a bounded Lua script. Redis message ownership is not
execution ownership: PostgreSQL leases, fencing and duplicate guards remain.
`EXECUTION_PENDING_CLAIM_BATCH_SIZE` now limits entries *scanned* (default 100,
maximum 1,000). Non-idle entries also advance the cursor, avoiding starvation.
The cursor advances with Python integers, not floating-point IDs or Redis 6.2
exclusive-range syntax. A full traversal wraps to `0-0`.

## Retention

Age-based deletion uses `XRANGE` and `XDEL`, not unsupported `XTRIM MINID`.
For work, the same Lua invocation calculates the minimum of the time cutoff,
every group's last-delivered ID, and each nonempty group's earliest Pending ID.
Only IDs strictly below the boundary are deleted. No groups or an unread group
mean work deletion is skipped. Concurrent group/claim operations cannot interleave
with boundary checking and deletion; explicit operator group resets after deletion
cannot restore already expired payloads.

Public events and the work DLQ retain their existing age policy. An Agent offline
beyond the event retention window must recover from durable history. The Agent's
own event DLQ remains Agent-owned.

- Each Lua call scans/deletes at most 1,000 entries; no unbounded scan.
- `EVENT_RETENTION_BATCH_SIZE` caps each Stream's total deletions per retention
  pass (default 1,000; larger values split into bounded calls).
- Backlogs are cleared gradually on subsequent passes. Age is eligibility for
  deletion, not a promise of immediate deletion or a hard memory ceiling.
- `XDEL` does not necessarily return memory for each individual entry immediately.
  Monitor actual Stream length and Redis memory; provision cleanup throughput
  above long-term ingress. Group count also contributes to work-boundary cost.
- Numeric Stream ID components are compared as decimal strings in Lua to avoid
  loss of precision above 2^53.

## Connection and permissions

The existing URL and credential mechanism remains unchanged. Use the primary
write endpoint, not a read-only replica. Service startup performs `INFO server`,
`COMMAND INFO`, and a read-only `EVAL` before DB-backed service initialization and
background tasks (after optional Alembic migration). Connection errors are not
treated as compatibility fallbacks. `/readyz` keeps its existing response shape;
it is not an exhaustive live ACL test on every request.

Allow `AUTH`/`SELECT` as applicable, `PING`, `INFO`, `COMMAND INFO`, `EVAL`, `EXISTS`,
`XADD`, `XGROUP CREATE`, `XREADGROUP`, `XPENDING`, `XCLAIM`, `XRANGE`, `XINFO GROUPS`,
`XACK`, and `XDEL` on Executor's configured Streams. Scripts use only these
standard commands, no server module or installation. Redis 6.0 and newer versions
can differ in scripting ACL enforcement; verify using the actual service account.
The preflight proves basic scripting access, not all per-key/per-command ACLs.

Test tooling additionally uses `DEL`, `XLEN`, `XREVRANGE`, and temporary-group
deletion. The outage smoke uses `CLIENT PAUSE <milliseconds>` (no 6.2 `ALL` option)
and must only run against an exclusive disposable server.

## Verification commands

Use a dedicated test server and test DB. Do not publish Redis 6.0.8 externally.
For example, provision a separate container without existing volumes:

```bash
docker run -d --name executor-redis608-compat-test \
  -p 127.0.0.1:16379:6379 redis:6.0.8-alpine \
  redis-server --save '' --appendonly no
export EXECUTOR_REDIS_TEST_URL=redis://127.0.0.1:16379/15
export EXECUTOR_EXPECT_REDIS_VERSION=6.0.8
uv run python scripts/quality_gate.py --integration
```

PowerShell can use the same Python command after setting `$env:EXECUTOR_REDIS_TEST_URL`
and `$env:EXECUTOR_EXPECT_REDIS_VERSION`; Docker is only one way of provisioning
the test Redis server. The scripts do not require it.

Real local Jupyter lifecycle smoke (needs local PostgreSQL CREATE DATABASE privilege):

```bash
export REDIS_COMPAT_JUPYTER_ENDPOINT=http://127.0.0.1:8888
export REDIS_COMPAT_JUPYTER_TOKEN='<local-test-token>'
export REDIS_COMPAT_RUNTIME_PROFILE=default
uv run python scripts/redis_compatibility_lifecycle_smoke.py
```

This creates a uniquely named temporary DB and Executor process, runs the existing
REST/MCP SINGLE retry/cancel and MULTI correction/finalize/cancel scenarios, and
checks DB/Outbox/Redis/results. It deletes only its own DB and Streams afterward.
It leaves result files and an Executor log under a printed temporary directory;
Jupyter-side notebooks/artifacts remain available for inspection. It does not
restart the running Executor or Jupyter services, and does not require an LLM.

## Local verification record — 2026-09-04

- Redis 6.0.8: official `redis:6.0.8-alpine`, no volume, loopback-only random
  port, isolated from the existing Compose Redis 7.4.10.
- Core unit gate: 569 passed, 4 existing opt-in Docker/Linux-identity skips.
- Real Redis integration: 27 passed on 6.0.8 and 27 passed on 7.4.10.
- Disposable PostgreSQL integration/migration tests: 30 passed.
- Separate test Agent with redis-py 5.3.1: 39 passed (no live LLM).
- Ruff lint/format and ty checks passed.
- Real Jupyter + temporary Executor/DB + Redis 6.0.8: all four existing lifecycle
  scenarios passed, including REST/MCP, DB state, Outbox events, Redis envelopes,
  runtime outputs and Artifact state checks:
  - SINGLE failure/retry: `dfffa418-5609-4b52-965f-a18286270732`
  - SINGLE running cancellation: `f1e86454-e497-459c-ad16-87c550ab0373`
  - MULTI correction/finalization: `433e6464-5b05-4f6d-b9a1-14d8144e766d`
  - MULTI running cancellation: `06948368-adb6-4118-8f4f-87ad6d2e8a4e`
- Evidence directory on the test machine:
  `/var/folders/jj/fc4wsmcj6x1d8vp23f0m9bzh0000gn/T/executor-redis-compat-soq1sw8q`.
  Its log includes intentionally injected code failures and cancellation
  diagnostics, but no Redis unsupported-command errors. The first smoke attempt
  failed before submission due to JSON instead of comma-separated allowlists in
  the new test runner; the runner was fixed before the successful rerun.
- Bounded local load sample: 30 concurrent claim calls with distinct 100-entry
  pages recovered 3,000 unique messages without duplication on both versions.
  Work retention deleted 2,999 ACKed entries, intentionally keeping the delivery
  boundary. Observed wall times: 6.0.8 claim 58.21 ms / retention 9.22 ms;
  7.4.10 claim 50.84 ms / retention 11.25 ms. This is a single loopback sample
  with small payloads, not a production SLA or a comparison to native XAUTOCLAIM.
- Existing Executor/Redis/Jupyter containers were not restarted. The E2E runner
  removed only its generated PostgreSQL databases and Redis keys; retained
  notebook/result evidence is test data. No production/Kubernetes test was run.
