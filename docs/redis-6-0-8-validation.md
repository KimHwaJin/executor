# Redis 6.0.8 acceptance validation

Date: 2026-09-05

## Result

The current Executor implementation passed exact-version Redis **6.0.8**
validation. No production code changes were needed.

- Tested commit: `f4d1d93` (`feature/executor-operational-hardening`).
- Combined PostgreSQL/Redis integration tests: **61 passed**, no failures or
  skips, 19.95 seconds.
- Additional disposable service-client acceptance checks: **2 passed**,
  0.99 seconds.

## Isolation and version evidence

- Official image: `redis:6.0.8`.
- Pulled manifest digest:
  `sha256:21db12e5ab3cc343e9376d655e8eabbdbe5516801373e95a8a9e66010c5b8819`.
- `INFO server` reported `redis_version:6.0.8`, standalone Linux aarch64.
- The test container exposed only a random localhost port, used a temporary
  `/data` filesystem, and had RDB/AOF persistence disabled.
- Limits: 1 CPU and 256 MiB memory. This is test setup, not sizing guidance.
- PostgreSQL tests created and removed uniquely named disposable databases.
- Redis tests used unique keys in test DB 15. No existing service Redis, Stream,
  consumer group, application database, Jupyter server, or workspace was reset.

## Covered behavior

The unchanged integration suites cover:

- Required Redis version/command checks and Lua execution.
- Consumer-group delivery, acknowledgements, stale pending reclamation,
  pagination, concurrent claim ownership, and missing-payload cleanup.
- Byte/decoded clients and Unicode payload preservation.
- Bounded retention, all consumer groups' pending/unread protection, concurrent
  reclamation versus deletion, and uint64 Stream ID comparisons.
- Invalid work-message DLQ handling without copying sensitive input payloads.
- Outbox publication and ordering, durable event/API agreement, multi-publisher
  concurrency, and backlog processing.
- PostgreSQL Worker ownership, fencing, capacity reservations, cancellation,
  maintenance, and the new Artifact concurrency protections.

Command statistics from the disposable Redis server confirmed actual use of
`EVAL` (38 calls), `XCLAIM` (23), and `XADD` (4,567) during this validation.
These are observed test counts, not throughput or latency benchmarks.

## Actual service-client acceptance checks

Two supplementary checks used `create_redis_client` and the current settings,
not only the direct Redis clients used by some integration fixtures:

1. Start `ApplicationContainer` against an isolated PostgreSQL database and
   Redis 6.0.8, run startup migration/compatibility checks, confirm readiness,
   submit an Execution, and verify both `operation.ready` in the work Stream
   and `PUBLISHED` in its DB Outbox row. Stop cleanly afterward.
   Runtime execution was disabled for this startup/transport check.
2. Authenticate an ephemeral ACL user, deny `EVAL`, and confirm compatibility
   checking fails with `NoPermissionError`. Grant `EVAL` to that same test user
   and confirm compatibility checking succeeds. Delete the user afterward.

These supplementary checks were temporary validation scripts, not changes to
the service or public API.

## Re-running the committed integration suites

Use a disposable Redis server and point the tests to its actual localhost port:

```bash
EXECUTOR_REDIS_TEST_URL=redis://127.0.0.1:<test-port>/15 \
EXECUTOR_EXPECT_REDIS_VERSION=6.0.8 \
EXECUTOR_RUN_POSTGRES_TESTS=1 \
EXECUTOR_REQUIRE_REDIS_TESTS=1 \
uv run pytest -q \
  tests/test_multi_worker_postgres.py \
  tests/test_event_delivery.py \
  tests/test_redis_streams_integration.py
```

The PostgreSQL fixture defaults to the local test administrator at port 5432.
Use `EXECUTOR_POSTGRES_TEST_ADMIN_URL` for another test PostgreSQL server with
permission to create and drop disposable databases. Do not use a production
administrator for this suite.

## Limits

- This validates functional compatibility, not the security patch status of
  Redis 6.0.8 or approval of the deployment's ACL/Lua policy.
- EVAL permission is still required. The startup check is not an exhaustive
  dry-run of every operational command under every ACL key pattern.
- No production Kubernetes/PV, actual five-day soak, or new live Jupyter E2E
  run was performed here. Runtime behavior in the integration fixtures does
  not establish long-running Jupyter reliability.
- The existing local Redis 7.4 service and other application containers were
  left running unchanged. The dedicated Redis 6.0.8 test container is removed
  after validation; its downloaded image may be reused for future checks.
