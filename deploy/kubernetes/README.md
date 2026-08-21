# Kubernetes deployment

This directory contains a production-oriented Executor deployment baseline. It intentionally does
not deploy PostgreSQL, Redis, Jupyter, Phoenix, an Ingress, or a PVC. Those are platform-owned
resources in the target environment.

## Resources

- `deployment.yaml`: one Executor API/Worker Pod with graceful shutdown and health probes
- `service.yaml`: internal REST and MCP (`/mcp`) ClusterIP endpoint
- `configmap.yaml`: non-secret settings that must be reviewed per environment
- `secret.example.yaml`: key names only; never commit a populated Secret
- `migration-job.yaml`: one release migration Job, separate from application Pods

The Executor image contains both the API and Redis consumer Worker. PostgreSQL remains the source of
truth, while Redis wakes Workers and carries outbound events. A Pod therefore needs connectivity to
PostgreSQL, Redis, and the registered Jupyter fleet.

## Required deployment substitutions

Before deployment:

1. Replace `executor-service:latest` in both workload manifests with the same immutable image
   digest or release tag.
2. Update MCP host/origin allowlists, tracing endpoint, database pool size, and resource limits in
   `configmap.yaml`.
3. Create `executor-secret` through the platform Secret manager with the keys shown in
   `secret.example.yaml`. A local `secret.yaml` is ignored by Git, but the platform Secret manager
   is preferred. `RUNTIME_CREDENTIAL_KEY` must be a Fernet key and must remain stable while
   encrypted Runtime credentials exist in PostgreSQL.
4. Change `executor-input-pvc` in `deployment.yaml` to the existing Agent/Executor shared claim.
   The Agent mounts it read-write; Executor mounts it read-only at `/workspace/input`.
5. Ensure all Jupyter servers mount their own common Jupyter PV. Executor must not mount or inspect
   that Jupyter PV; notebook and artifact access goes through Jupyter APIs.
6. After Executor is Ready, register every Jupyter server through the Runtime Target REST or MCP
   API. Executor starts with an empty Runtime Fleet and never creates a target from environment
   variables.

The manifest intentionally omits Runtime/Jupyter tuning variables and uses the application defaults.
Add a setting to the ConfigMap only when the deployment needs an explicit environment override.

Generate a Fernet key outside Git with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

`MCP_ALLOWED_HOSTS` must contain every Host header that can reach `/mcp`, including the internal
Service and Gateway/Ingress hostnames. `MCP_ALLOWED_ORIGINS` must contain the browser or Agent
origins that send an Origin header. Keep both lists narrow; do not disable DNS-rebinding protection.

## Release order

Run the migration once per release before rolling out the Deployment. Do not run Alembic in every
application Pod.

```bash
kubectl apply -f deploy/kubernetes/configmap.yaml
kubectl apply -f deploy/kubernetes/service.yaml
kubectl apply -f deploy/kubernetes/migration-job.yaml
kubectl wait --for=condition=complete job/executor-migrate --timeout=300s
kubectl apply -f deploy/kubernetes/deployment.yaml
kubectl rollout status deployment/executor --timeout=300s
```

For a later release, the platform must create a fresh migration Job name or remove the completed
Job before applying the new release manifest. A fixed, already-completed Job does not execute again.

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

## Shutdown and scaling

The manifest uses one replica by default. Executor supports multiple replicas, but every Pod must
share PostgreSQL, Redis, the consumer group, credential key, input PVC, and Runtime Target registry.
The Downward API assigns the Pod name to `EXECUTION_CONSUMER_NAME` so replicas do not share worker
identities.

The 90-second Pod grace period is greater than the default 30-second execution drain plus 20-second
shutdown cleanup and buffer. If those settings increase, increase `terminationGracePeriodSeconds`
as well. A live Jupyter WebSocket cannot migrate between Pods during a rollout; work that outlives
the drain period follows the documented failure and explicit retry flow.
