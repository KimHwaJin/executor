# Kubernetes deployment (Deployment-only settings)

This directory contains a production-oriented Executor deployment baseline. It intentionally does
not deploy PostgreSQL, Redis, Jupyter, Phoenix, an Ingress, or a PVC. Those are platform-owned
resources in the target environment.

## Resources

- `deployment.yaml`: one Executor API/Worker Pod; settings come from the `config*.yml` files in
  the image (only `APP_ENV` is set here), automatic startup migration, graceful shutdown and
  health probes
- `service.yaml`: optional reference for the platform's internal REST/MCP Service

No ConfigMap, Secret, or migration Job is required. If only Deployment creation is
allowed, the platform must supply the existing Service/routing and PVC. A Deployment
alone does not create a stable network endpoint, database, Redis server, or volume claim.

The Executor image contains both the API and the Worker. PostgreSQL is the source of truth and
Workers find work by polling it; Redis only carries outbound events. A Pod therefore needs connectivity to
PostgreSQL, Redis, and the registered Jupyter fleet.

Redis server 6.0.8 or newer is required by this branch. Build the image with its
updated `uv.lock` (redis-py 5.3.x); changing only the Redis URL does not update code
inside an older image. Permit `INFO`, `COMMAND INFO`, `EVAL`, and the Stream commands
listed in [Redis compatibility](../../docs/redis-6-compatibility.md). Pods start and
stay ready while Redis is down; events wait in the Outbox and publish once Redis returns.
The retention pass checks the Redis version and commands before its first trim and logs an
error and skips Redis trimming while the check fails. No Redis data reset is required.

## Required deployment substitutions

Before deployment:

1. Copy `deployment.yaml` to the Git-ignored `deployment.local.yaml`, or render it
   privately in CI/CD. Replace `executor-service:latest` with an immutable image digest/tag.
2. Settings live in `config.yml` (shared tunables at their code defaults) and `config.prd.yml`
   (per environment) inside the image; the Deployment sets only `APP_ENV=prd`. Edit database/Redis
   URLs, MCP host/origin allowlists and storage roots in `config.prd.yml`. To change a shared value
   such as Runtime profiles or pool size for one environment, add the key to `config.prd.yml`; for
   every environment, change it in `config.yml` and `src/executor_service/settings.py` together.
   An environment variable with the same name still overrides a file value.
3. Replace `REPLACE_USER`, `REPLACE_PASSWORD`, and `REPLACE_WITH_FERNET_KEY` in that file.
   `RUNTIME_CREDENTIAL_KEY` must be a Fernet key and must remain stable while encrypted
   Runtime credentials exist in PostgreSQL. URL-encode special characters in URL credentials.
4. Change `executor-shared-pvc` in `deployment.yaml` to the existing Agent/Executor RWX claim.
   The PVC is mounted at `/mnt/data`. The project root (`/mnt/data/dtest`) holds one folder per
   service: `INPUT_STORAGE_ROOT=/mnt/data/dtest` so callers send `batch/inputs/...` or
   `agent/...`, and `SHARED_STORAGE_ROOT=/mnt/data/dtest/executor` for results. Executor must be able to read
   the Agent directory and write its own result directory. Agent must resolve every result/manifest
   reference against its own mount of the Executor directory, not its authoring directory.
   Do not change the result root for existing executions unless the existing result files are
   also available there; files and DB references are not moved automatically.
5. Ensure all Jupyter servers mount their own common Jupyter PV. Executor must not mount or inspect
   that Jupyter PV; notebook and artifact access goes through Jupyter APIs.
6. Replace the placeholder `AUTH_CLIENTS` hashes in `config.prd.yml` with the SHA-256 of each
   caller's real key (`printf %s "$KEY" | sha256sum`). Hand each key to its caller out of band;
   only the hash ships in the image. During rotation list both hashes, then drop the old one.
7. After Executor is Ready, register every Jupyter server through the Runtime Target REST API
   with the `ops` admin key. Executor starts with an empty Runtime Fleet and never creates a
   target from environment variables. MCP is disabled in stg and prd.

The manifest intentionally contains no default Runtime Target endpoint or Jupyter token. Register
targets after startup through the Runtime Target API. Generic fleet limits are explicit in the
Deployment; provider-specific timeouts use application defaults unless an environment overrides
them.

**Security tradeoff:** inline credentials are visible to anyone who can read the Deployment
or its Pod specification, and may appear in CI logs and Kubernetes revision history.
Never commit the populated manifest, paste it into tickets, or print it in a build log.
Restrict deployment read permissions and protect rendered CI artifacts. The checked-in
manifest contains placeholders only. Environment changes require Pod recreation.

Generate a Fernet key outside Git with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

stg and prd run `AUTH_MODE: api_key` and `MCP_ENABLED: false`. If MCP is ever enabled,
`MCP_ALLOWED_HOSTS` must contain every Host header that can reach `/mcp`, including the internal
Service and Gateway/Ingress hostnames. `MCP_ALLOWED_ORIGINS` must contain the browser or Agent
origins that send an Origin header. Keep both lists narrow; do not disable DNS-rebinding protection.

## Release order

`DB_AUTO_MIGRATE=true` runs `alembic upgrade head` during application startup before
DB initialization, Outbox, Worker or HTTP serving. Each start checks the head; an already
current database is a no-op. Failures stop startup, release the transaction lock, and log
a safe error type/SQLSTATE without connection secrets or SQL parameters.

The database and login must already exist. The account must be able to create/alter
Executor schema objects and own the tables it migrates. Without these privileges,
set `DB_AUTO_MIGRATE=false` and arrange a privileged manual migration before startup.

All new automatic and CLI migrations share a PostgreSQL advisory transaction lock.
`DB_MIGRATION_LOCK_TIMEOUT_SECONDS=60` bounds lock waits; `DB_MIGRATION_STATEMENT_TIMEOUT_SECONDS=300`
bounds each SQL statement, not the total release duration. DB connect timeout is separate.
The 600-second startup probe budget includes migration and Worker initialization; tune
it for the total duration of your migrations. A failed Pod does not serve traffic.

The manifest uses **RollingUpdate** (`maxSurge: 1`, `maxUnavailable: 0`). Migrations must stay
backward compatible so old and new Pods can share the schema during the rollout. A stopping Pod
drains, then hands its running Steps to another Pod instead of failing them; the kernel keeps
running and the new owner replays the Step journal. See
[Step handover](../../docs/execution-recovery.md#step-handover-between-workers).
The DB lock only serializes migrations, not old application queries. Never run old CLI migration
code concurrently: it does not know about the new lock.

The first rollout to `0006` is the exception: older Pods cannot hand over, and their Jupyter
servers need the journal extension. Drain through the maintenance API, check safe-to-shutdown,
deploy the Jupyter extension and Executor, then activate admission. A schema change that is not
backward compatible still needs this drain, or `Recreate` for that release. `0011` is one: it drops
`executions.task_id` and `workflow_id`, which `0010` Pods still write.

Rolling out `0011` from `0010`:

1. Drain admission through `POST /api/v1/maintenance/drain` and wait until
   `GET /api/v1/maintenance` reports it is safe to shut down.
2. In `deployment.local.yaml`, set `strategy.type: Recreate` and remove the `rollingUpdate` block,
   or scale the Deployment to 0 replicas before switching the image.
3. Deploy the new image; the first Pod migrates on startup.
4. Restore `RollingUpdate` (or the replica count) and activate admission through
   `POST /api/v1/maintenance/activate`.

`0012` (caller authentication) is another exception. It makes `executions.tenant_id` NOT NULL,
moves workspaces under `tenants/{tenant}/`, stops writing the shared `executor.events` Stream, and
changes the API contract for every caller. It ships from an empty database and shared PV,
together with pr-agentic-service and pr-batch: reset PostgreSQL and the Executor PV, deploy with
`Recreate`, then register Runtime Targets with the `ops` key.

New migrations follow expand/contract instead: a column is dropped only in the release after the
code stopped using it, so ordinary releases keep `RollingUpdate`.

Current head is `0011`. Runtime purge preserves
historical IDs after deletion.
See [purge and rollback](../../dev_docs/post-runtime-target-purge.md).
Before rollback, review whether the old image is compatible with the new schema; startup
does not automatically downgrade.

```bash
kubectl apply -n <namespace> -f deploy/kubernetes/deployment.local.yaml
kubectl rollout status -n <namespace> deployment/executor --timeout=660s
kubectl logs -n <namespace> deployment/executor -c executor
```

This deployment path requires no Kubernetes API access from inside the application.

## Probes and endpoints

- Startup/liveness: `GET /healthz` checks that the process serves HTTP.
- Readiness: `GET /readyz` requires the expected database schema and a Worker accepting
  work. Redis is reported as `redis_reachable` but does not gate readiness, because the Outbox
  retries event publication. An empty Runtime Fleet does not make the control API unready.
- REST/OpenAPI: `GET /docs`, `GET /redoc`, `GET /openapi.json`
- MCP Streamable HTTP: `POST /mcp`
- Internal Service URL: `http://executor:8000`

If the Pod is running but not Ready, inspect `/readyz`; a missing migration or a
draining Worker is intentionally reported there. Inspect `/api/v1/runtime-targets` separately for
Runtime Fleet health.

After migration, `alembic_version.version_num` is `0006`. Do not stamp an empty database;
startup must execute `alembic upgrade head` so it creates constraints, indexes, and the initial
Executor maintenance row.

## Shutdown and scaling

The manifest uses one replica by default. Executor supports multiple replicas, but every Pod must
share PostgreSQL, Redis, credential key, input PVC, and Runtime Target registry. Each Worker names
itself `<hostname>-<pid>-<suffix>`, so replicas never share a lease owner identity.

The 90-second Pod grace period is greater than the default 30-second execution drain plus 20-second
shutdown cleanup and buffer. If those settings increase, increase `terminationGracePeriodSeconds`
as well. A Step that outlives the drain period is handed over to another Pod; a Step with nothing
to hand over (for example one still preparing its Runtime) follows the documented `WORKER_SHUTDOWN`
failure and explicit retry flow. Scale-in under HPA uses the same path.

## Non-production Worker-loss validation

The isolated Docker Worker-loss gate validates application recovery without touching Kubernetes.
For initial cluster qualification or major Deployment changes, the repository also provides a
guarded platform validator that force deletes only the Pod recorded as the running Attempt owner:

```bash
uv run python scripts/kubernetes_worker_failover_e2e.py \
  --base-url https://executor.example.internal \
  --context non-production-cluster \
  --namespace executor-test \
  --deployment executor \
  --allow-pod-delete
```

The command is destructive and refuses to run without `--allow-pod-delete`. It verifies
`LEASE_EXPIRED`, Runtime cleanup, explicit `FROM_START` retry, final success, and durable event
uniqueness/order. See [Executor resilience testing](../../docs/executor-resilience-testing.md) for
prerequisites, optional authentication, report location, and exact pass criteria.
