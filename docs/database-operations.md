# Database Operations

Executor uses a bounded SQLAlchemy `AsyncAdaptedQueuePool` for application traffic. Alembic keeps
its independent `NullPool`, so a migration job opens only its migration connection and does not
consume an application pool.

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

Migration `0003` adds indexes for the unfiltered Execution cursor list and retained-session cleanup.
Existing indexes cover maximum-runtime expiry, status lists, worker lease recovery, Runtime Target
capacity, Outbox publication, and child history pagination.

After applying migrations to a local PostgreSQL database, verify the critical plans with:

```bash
uv run alembic upgrade head
uv run python scripts/postgres_query_plan_smoke.py
```

The script runs read-only `EXPLAIN ANALYZE` statements. It disables sequential scans only inside its
own transaction because a small development database would otherwise correctly prefer a sequential
scan. The check proves that the intended indexes are usable; production query planning remains
fully controlled by PostgreSQL statistics.
