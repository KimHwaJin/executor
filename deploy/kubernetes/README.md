# Kubernetes deployment (Deployment-only settings)

This directory contains a production-oriented Executor deployment baseline. It intentionally does
not deploy PostgreSQL, Redis, Jupyter, Phoenix, an Ingress, or a PVC. Those are platform-owned
resources in the target environment.

## Resources

- `deployment.yaml`: one Executor API/Worker Pod, inline environment settings,
  automatic startup migration, graceful shutdown and health probes
- `service.yaml`: optional reference for the platform's internal REST/MCP Service

No ConfigMap, Secret, or migration Job is required. If only Deployment creation is
allowed, the platform must supply the existing Service/routing and PVC. A Deployment
alone does not create a stable network endpoint, database, Redis server, or volume claim.

The Executor image contains both the API and Redis consumer Worker. PostgreSQL remains the source of
truth, while Redis wakes Workers and carries outbound events. A Pod therefore needs connectivity to
PostgreSQL, Redis, and the registered Jupyter fleet.

## Required deployment substitutions

Before deployment:

1. Copy `deployment.yaml` to the Git-ignored `deployment.local.yaml`, or render it
   privately in CI/CD. Replace `executor-service:latest` with an immutable image digest/tag.
2. Edit `containers[0].env`: database/Redis URLs, MCP host/origin allowlists, Runtime
   profiles, storage root, migration timeouts and pool size. Edit resource limits as needed.
3. Replace `REPLACE_USER`, `REPLACE_PASSWORD`, and `REPLACE_WITH_FERNET_KEY` privately.
   `RUNTIME_CREDENTIAL_KEY` must be a Fernet key and must remain stable while encrypted
   Runtime credentials exist in PostgreSQL. URL-encode special characters in URL credentials.
4. Change `executor-shared-pvc` in `deployment.yaml` to the existing Agent/Executor RWX claim.
   Both services mount it read-write at their configured shared-storage root; Executor uses
   `/workspace/shared`. The Agent must resolve Executor `result_ref.relative_path` values against
   its own mount of the same claim.
5. Ensure all Jupyter servers mount their own common Jupyter PV. Executor must not mount or inspect
   that Jupyter PV; notebook and artifact access goes through Jupyter APIs.
6. After Executor is Ready, register every Jupyter server through the Runtime Target REST or MCP
   API. Executor starts with an empty Runtime Fleet and never creates a target from environment
   variables.

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

The manifest uses **Recreate**: old Pods stop before replacement Pods start, allowing
schema-changing releases at the cost of a brief outage. First drain through the existing
maintenance API and check safe-to-shutdown. Recreate does not preserve live WebSockets or
long-running work; do not force an in-progress analysis through a schema-changing rollout.
The DB lock only serializes migrations, not old application queries. Stop any old Executor
processes outside this Deployment too. Never run old CLI migration code concurrently: it
does not know about the new lock. After a manual drain, activate admission after deployment.

Current head is `0004`; no schema revision is added for the auto-migrate feature itself.
Before rollback, review whether the old image is compatible with the new schema; startup
does not automatically downgrade. See [trace removal](../../docs/opentelemetry-removal.md).

```bash
kubectl apply -n <namespace> -f deploy/kubernetes/deployment.local.yaml
kubectl rollout status -n <namespace> deployment/executor --timeout=660s
kubectl logs -n <namespace> deployment/executor -c executor
```

This deployment path requires no Kubernetes API access from inside the application.

## Probes and endpoints

- Startup/liveness: `GET /healthz` checks that the process serves HTTP.
- Readiness: `GET /readyz` requires the expected database schema, Redis, and a Worker accepting
  work. An empty Runtime Fleet does not make the control API unready.
- REST/OpenAPI: `GET /docs`, `GET /redoc`, `GET /openapi.json`
- MCP Streamable HTTP: `POST /mcp`
- Internal Service URL: `http://executor:8000`

If the Pod is running but not Ready, inspect `/readyz`; a missing migration, Redis outage, or
draining Worker is intentionally reported there. Inspect `/api/v1/runtime-targets` separately for
Runtime Fleet health.

After migration, `alembic_version.version_num` is `0004`. Do not stamp an empty database;
startup must execute `alembic upgrade head` so it creates constraints, indexes, and the initial
Executor maintenance row.

## Shutdown and scaling

The manifest uses one replica by default. Executor supports multiple replicas, but every Pod must
share PostgreSQL, Redis, the consumer group, credential key, input PVC, and Runtime Target registry.
The Downward API assigns the Pod name to `EXECUTION_CONSUMER_NAME` so replicas do not share worker
identities.

The 90-second Pod grace period is greater than the default 30-second execution drain plus 20-second
shutdown cleanup and buffer. If those settings increase, increase `terminationGracePeriodSeconds`
as well. A live Jupyter WebSocket cannot migrate between Pods during a rollout; work that outlives
the drain period follows the documented failure and explicit retry flow.

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
