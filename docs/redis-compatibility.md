# Redis compatibility

## Requirements

- Server: Redis 8.0 or newer, verified on 8.0.6, 8.2.11, 8.4.8, 8.6.8, 8.8.4 and 8.10.3.
  Redis 8 publishes only even minor versions as GA; `8.5.x` is the 8.6 release candidate and is
  not for production. Python client: redis-py 7.4.x (`redis>=7.4,<8`), frozen at 7.4.1 in both
  Executor and test Agent lockfiles. redis-py 8 switches the wire protocol to RESP3 by default
  and is not used.
- Executor uses only standard Stream commands and RESP2: no Lua, Functions, modules, Pub/Sub or
  transactions.

## Local Compose

`docker-compose.infra.yml` and `compose.worker-failover.yaml` pin `redis:8.6.8` with AOF enabled
on port 6379. The shared Compose volume reuses external volume `executor_executor-redis-8`
(override with `SHARED_REDIS_VOLUME`); create it once with
`docker volume create executor_executor-redis-8`. The integration quality gate expects 8.6.8
unless `EXECUTOR_EXPECT_REDIS_VERSION` says otherwise. `REDIS_URL` never selects a server
version, and Redis outside local Compose stays the infrastructure operator's responsibility.

Redis 8 reads a Redis 6 RDB/AOF volume, so an existing volume can be upgraded in place, but a
volume written by Redis 8 cannot go back to an older server. Keeping old events or Agent consumer
positions across a fresh volume needs a separately reviewed logical migration with writers and
consumers stopped.

## Retention

Each retention pass trims every per-tenant `executor.events.{tenant_id}` Stream with one exact
`XTRIM <stream> MINID <cutoff>`: only IDs strictly below the age boundary are deleted. Workers
find work through PostgreSQL, so retention never touches work state. An Agent offline beyond the
event retention window must recover from durable history. The Agent's own event DLQ remains
Agent-owned.

- Exact trimming frees whole old Stream nodes and edits at most the boundary node, so its cost
  follows the entries it removes. Approximate `~` trimming with `LIMIT` is not used: a `LIMIT`
  below a node's entry count (`stream-node-max-entries`, default 100) would remove nothing.
- `EVENT_RETENTION_BATCH_SIZE` bounds PostgreSQL deletions only.
- Monitor actual Stream length and Redis memory; age is eligibility for deletion, not a hard
  memory ceiling.

## Standalone and Sentinel

`REDIS_MODE` selects how Executor finds Redis; nothing else in the code differs between the two.

| `REDIS_MODE` | Reads | Connects to |
| --- | --- | --- |
| `standalone` (default; dev, stg, local) | `REDIS_URL` | that host, with the DB in the URL |
| `sentinel` (prd) | `REDIS_SENTINEL_HOST`, `_PORT`, `_MASTER_NAME`, `_USERNAME`, `_AUTH` | the master Sentinel reports, DB 0 |

- Every Executor command is a write, so it must reach the master. In `sentinel` mode each new
  connection asks Sentinel for the current master; after a failover the old connections fail and
  the next one lands on the new master without a restart. Never point `REDIS_URL` at one node of
  a Sentinel deployment: it works only until the first failover.
- `REDIS_SENTINEL_AUTH` holds the AES-encrypted password required by the internal guide and is
  decrypted with `hcp.framework.util.crypto_cipher.AESCipher`; `sentinel` mode therefore needs the
  internal `hcp` library and fails at startup with a clear error without it. One account signs in
  to both Sentinel and Redis. `REDIS_SENTINEL_MASTER_NAME` is required; Executor does not guess it.
- Sentinel must report a master address that Executor Pods can reach.
- Verified against Redis 8.6.8 with one master, two replicas and three ACL-protected Sentinels:
  killing the master cost one failed `XADD` and about five seconds; a planned
  `SENTINEL FAILOVER` cost none. The Outbox retries the failed event.
- Redis replication is asynchronous. An event acknowledged just before a failover can be missing
  from the new master, and a consumer group can step back slightly. Consumers already deduplicate
  by `event_id` and fill gaps from the durable event history; they should also re-check an
  Execution that has been silent too long. `min-replicas-to-write` on the server narrows the
  window.

## Connection and permissions

The existing URL and credential mechanism remains unchanged. Use the primary write endpoint, not
a read-only replica. Startup and `/readyz` never need Redis: the Outbox keeps events until Redis
returns, and `/workerz` reports `redis_reachable`. While Redis is down or denies `XTRIM`, the
retention pass still cleans PostgreSQL, logs a warning, and trims on a later pass.

Allow `AUTH`/`SELECT` as applicable, `PING`, `XADD`, and `XTRIM` on Executor's event Streams.
Use `maxmemory-policy noeviction` or a `volatile-*` policy: the event Streams have no TTL, and an
`allkeys-*` policy can evict a whole Stream under memory pressure.

Test tooling additionally uses `INFO`, `EXISTS`, `DEL`, `XLEN`, `XRANGE`, `XREVRANGE`, consumer
group commands, and temporary-group deletion. The outage smoke uses `CLIENT PAUSE <milliseconds>`
and must only run against an exclusive disposable server.

## Verification commands

Use a dedicated test server and test DB. Do not publish the test Redis externally.
For example, provision a separate container without existing volumes:

```bash
docker run -d --name executor-redis-compat-test \
  -p 127.0.0.1:16379:6379 redis:8.6.8-alpine \
  redis-server --save '' --appendonly no
export EXECUTOR_REDIS_TEST_URL=redis://127.0.0.1:16379/15
export EXECUTOR_EXPECT_REDIS_VERSION=8.6.8
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
