# frodo deployment

Plain manifests for the frodo cluster. Every setting, credentials included, lives in
`config.yml` and `config.dev.yml` baked into the image; the Deployment sets only `APP_ENV=dev`.

| File | Resource |
| --- | --- |
| `deployment.yaml` | Executor API and Worker, one container |
| `service.yaml` | `executor:8000` inside the cluster |
| `virtualservice.yaml` | `executor.frodo.com` through the Istio ingress Gateway |

## Before the first apply

1. Fill in `config.dev.yml` outside Git (database, Redis, Fernet key, `AUTH_CLIENTS` hashes,
   MCP hosts) and build with `docker build --build-arg APP_ENV=dev .`.
2. `deployment.yaml`: the image (`REPLACE_REGISTRY/executor-service:REPLACE_TAG`) and, if it
   differs, the RWX claim name.
3. `virtualservice.yaml`: the Gateway, and the host if it is not `executor.frodo.com`.

```bash
kubectl apply -n <namespace> -f deploy/frodo/
kubectl rollout status -n <namespace> deployment/executor --timeout=660s
```

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
