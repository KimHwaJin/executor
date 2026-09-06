# Local Redis default: 6.0.8

Date: 2026-09-05

## Configuration

- `docker-compose.yml` pins the server image to `redis:6.0.8`.
- `compose.worker-failover.yaml` uses the same image and a separate
  `redis-data-608` volume for disposable failover tests.
- Redis keeps AOF persistence enabled and port 6379 unchanged.
- The new Compose volume is `executor-redis-608`; on this machine its Docker
  name is `executor_executor-redis-608`.
- `REDIS_URL` remains a connection URL, not a server-version setting.
- The integration quality gate requires server version 6.0.8 by default.
  Set `EXECUTOR_EXPECT_REDIS_VERSION` explicitly for cross-version validation.
- This does not downgrade Redis managed outside local Compose. Kubernetes and
  other external Redis servers remain infrastructure-operator responsibilities.

## Local switch and data preservation

The previously running local server was Redis 7.4. Its persisted AOF/RDB files
were **not** opened with Redis 6.0.8. The old Docker volume
`executor_executor-redis` remains detached and preserved.

No Execution was active at the switch. Executor was stopped briefly to prevent
publication and retention changes during a one-time logical migration.
All source keys were non-expiring Streams, and all groups had zero pending
messages. A separate temporary Redis 6.0.8 container populated the new volume.

| Stream | Entries | Consumer groups |
| --- | ---: | ---: |
| `executor.events` | 652 | 0 |
| `executor.work` | 8 | 1 |
| `executor.work.multi-smoke.a1cb589da7234f1ab1eb7c8cd22c600c` | 1 | 1 |

Verification compared all entry IDs and raw fields, Stream last-generated IDs,
group last-delivered IDs, and consumer names between both servers. All matched.
Consumer idle times and version-specific diagnostic counters are not preserved.
Empty consumer records may disappear across the Redis 6 restart and be
re-created on subsequent consumption; group positions and pending-free state
remain intact. This is not a loss of work messages.
The new server then persisted its own compatible data before Compose replaced
the old Redis container. The temporary container was removed.

Executor was rebuilt from the current Redis-6-compatible source and restarted.
PostgreSQL data, Jupyter containers, and their workspaces were not reset or
recreated. Startup reconciliation reported zero recovered Executions and zero
runtime cleanup targets.

## Starting from another checkout

For a new installation, `docker compose up -d redis` starts 6.0.8 with its
dedicated volume. Repeated starts reuse that same volume.

On an existing Redis 7 installation, merely changing the volume starts an
**empty** Redis. If events and group positions must be preserved, stop writers
and consumers and perform a separately reviewed logical migration first.
The local migration above handled only Streams with no pending messages; it
is not a general-purpose migration procedure for arbitrary Redis data.

Do not attach the old Redis 7 volume to Redis 6.0.8, and do not run
`docker compose down -v` when data must be retained. The detached old volume is
a point-in-time recovery source for Redis 7, not a live backup: switching back
after new work has occurred requires reconciling the new data and PostgreSQL.

## Verification

`INFO server` returned `redis_version:6.0.8` after the real Compose replacement.
Executor and Redis container health checks passed; `/healthz` returned `ok`,
`/readyz` returned `ready` with PostgreSQL, Redis, and Worker accepting checks
all true, and `/workerz` returned `ACCEPTING` with zero active Executions.

Validation after switching the real local server:

- `uv run python scripts/quality_gate.py --integration`: passed.
- Ruff lint/format and `ty check`: passed.
- Base regression suite: 588 passed, 4 skipped (opt-in live/container tests).
- Redis integration: 27 passed; PostgreSQL integration/migrations: 34 passed.
- After also updating the failover Compose default, all 4 dedicated default
  configuration tests passed, lint/format/type checks were repeated, and both
  Compose files passed `docker compose config --quiet`.

The default configuration tests cover exact image tags, separate volume names,
and the integration gate's expected-version default and explicit override.
Integration tests used unique keys in Redis DB 15 and disposable PostgreSQL
databases; the service continues using Redis DB 0 and its original database.

Functional compatibility does not imply Redis 6.0.8 is security-patch current.
Deployment ACL/Lua permissions and security approval remain separate checks.
This setup change does not constitute a new live Jupyter E2E or long-soak run.
