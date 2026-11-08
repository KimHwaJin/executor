# frodo deployment

Plain manifests for the frodo cluster. Settings come from `config.yml` in the image, the
`executor-config` ConfigMap (mounted as `/app/config.dev.yml`, `APP_ENV=dev`) and the
`executor-secrets` Secret (environment variables, which win over both files).

| File | Resource |
| --- | --- |
| `deployment.yaml` | Executor API and Worker, one container |
| `service.yaml` | `executor:8000` inside the cluster |
| `virtualservice.yaml` | `executor.frodo.com` through the Istio ingress Gateway |
| `configmap.yaml` | frodo values without credentials |
| `secret.example.yaml` | template for `secret.yaml` (Git-ignored) |

## Before the first apply

1. `deployment.yaml`: the image (`REPLACE_REGISTRY/executor-service:REPLACE_TAG`) and, if it
   differs, the RWX claim name.
2. `virtualservice.yaml`: the Gateway, and the host if it is not `executor.frodo.com`.
3. `configmap.yaml`: the three `AUTH_CLIENTS` hashes ([API Keys](../../docs/api-keys.md)).
4. `cp secret.example.yaml secret.yaml` and fill in the database, Redis and Fernet values.

```bash
kubectl apply -n <namespace> -f deploy/frodo/secret.yaml
kubectl apply -n <namespace> -f deploy/frodo/configmap.yaml \
  -f deploy/frodo/service.yaml -f deploy/frodo/virtualservice.yaml \
  -f deploy/frodo/deployment.yaml
kubectl rollout status -n <namespace> deployment/executor --timeout=660s
```

Pods read the ConfigMap and Secret only at startup; after changing either, run
`kubectl rollout restart -n <namespace> deployment/executor`.

## After the Pod is Ready

- `GET /workerz` with the `ops` key must show `redis_reachable: true`: Redis does not gate
  readiness, so a wrong Redis password still turns the Pod Ready.
- Register every Jupyter server through the Runtime Target API with the `ops` key; Executor
  starts with an empty Runtime Fleet.

## Scaling

The platform's CPU HPA owns the replica count, so the Deployment sets none. Each Pod opens up to
16 PostgreSQL connections, shared with pr-agentic-service and pr-batch on one server; keep the HPA
maximum within that budget ([pool settings](../../docs/database-operations.md)). Scale-in and
rollouts drain the stopping Pod and hand its running Steps to another Pod within the 90-second
grace period; see [deploy/kubernetes](../kubernetes/README.md#shutdown-and-scaling).
