# Database Operations

Executor uses a bounded SQLAlchemy `AsyncAdaptedQueuePool` for application traffic. Alembic keeps
its independent `NullPool`, so a migration job opens only its migration connection and does not
consume an application pool.

## Schema baseline

Revision `0001` is a complete snapshot of the Executor schema recorded on 2026-08-26. It creates
all current tables, foreign keys, check/unique constraints, and operational indexes in one step.
The earlier incremental development revisions were deliberately removed; this is a pre-release
baseline reset, not a data-preserving upgrade from that discarded chain.

For a new empty database:

```bash
uv run alembic upgrade head
uv run alembic current
uv run alembic check
```

Revision `0001` includes internal monotonic fencing tokens, exclusive cancellation ownership,
Runtime abort state and observations, the shared-result reference contract, Executor-wide
maintenance admission, and durable leased Maintenance Runs with per-Execution targets. It is the
only revision and current head. `current` must report `0001 (head)`, and `check` must report that
no new upgrade operations are detected. A development database carrying one of the removed
pre-baseline revisions must be backed up if its data matters, then recreated as an empty database
before `upgrade head`. Clear the four Executor Redis Streams at the same time so stale work
messages cannot reference rows removed by the reset. Do not use this reset procedure for a
production database.

The opt-in PostgreSQL suite creates a fresh database per test, applies the real Alembic baseline,
runs `alembic check`, and only then executes the concurrency scenario:

```bash
EXECUTOR_RUN_POSTGRES_TESTS=1 uv run pytest tests/test_multi_worker_postgres.py
```

## Pool settings

| Environment variable | Default | Meaning |
| --- | ---: | --- |
| `DATABASE_POOL_SIZE` | `10` | Persistent connections maintained by one Executor process |
| `DATABASE_MAX_OVERFLOW` | `5` | Temporary connections allowed above the persistent pool |
| `DATABASE_POOL_TIMEOUT_SECONDS` | `30` | Maximum wait for a pool checkout before failing |
| `DATABASE_POOL_RECYCLE_SECONDS` | `1800` | Age after which a checked-out connection is replaced |
| `DATABASE_CONNECT_TIMEOUT_SECONDS` | `10` | Maximum time for a new PostgreSQL connection |

Every checkout uses `pool_pre_ping`, so a connection closed by PostgreSQL or infrastructure is
discarded before application work uses it. Long-running Jupyter execution does not hold a database
connection: Executor checks sessions out only for bounded state transitions, queries, heartbeat,
and Outbox work.

The maximum connection budget for Executor is:

```text
executor_max_connections = executor_pod_count * (DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW)
```

With the defaults, one Pod can open at most 15 connections and two Pods at most 30. Keep the sum of
all Executor pools, other services, migration jobs, and operator connections below PostgreSQL
`max_connections`. Reserve at least 20% of the server limit for administration and unexpected
load. If PostgreSQL allows 100 connections and 20 are reserved, five default-sized Executor Pods
would be the safe upper bound only when no other application shares that database:

```text
floor((100 - 20) / 15) = 5 pods
```

Pool capacity is not the same as the number of concurrently running analyses. Jupyter cells may run
for days without holding a PostgreSQL connection, so size the pool from concurrent short database
transactions and Pod count rather than from total active analyses.

## Query-plan verification

Baseline `0001` includes indexes for the unfiltered Execution cursor list, retained-session cleanup,
maximum-runtime expiry, status lists, execution and cancellation lease recovery, Runtime Target
capacity, Outbox publication, child history pagination, and Maintenance Run recovery.

After applying migrations to a local PostgreSQL database, verify the critical plans with:

```bash
uv run alembic upgrade head
uv run python scripts/postgres_query_plan_smoke.py
```

The script runs read-only `EXPLAIN ANALYZE`
statements. It disables sequential scans only
inside its own transaction because a small development database would otherwise correctly prefer
a sequential scan. The check proves that the intended indexes are usable; production query
planning remains fully controlled by PostgreSQL statistics.
