# Jupyter Pool Operations

Executor uses two strict Jupyter scheduling pools: `INTERACTIVE` for user-driven analysis and
`BATCH` for promoted Workflow runs. Pool selection is persisted on the Execution. The scheduler
never falls back across pools.

## Local topology

All local Jupyter containers use `jupyter/datascience-notebook:latest`, mount the same
`./notebook_dir:/workspace/pv` shared-PV contract, and load the same Jupyter server configuration.

| Service | Pool | Host endpoint | Default token variable |
|---|---|---|---|
| `jupyter` | INTERACTIVE | `http://127.0.0.1:8888` | `JUPYTER_TOKEN` |
| `jupyter-secondary` | INTERACTIVE | `http://127.0.0.1:8889` | `JUPYTER_SECONDARY_TOKEN` |
| `jupyter-batch-primary` | BATCH | `http://127.0.0.1:8890` | `JUPYTER_BATCH_PRIMARY_TOKEN` |
| `jupyter-batch-secondary` | BATCH | `http://127.0.0.1:8891` | `JUPYTER_BATCH_SECONDARY_TOKEN` |

Start the full fleet:

```bash
docker compose --profile multi-jupyter --profile batch-jupyter up -d --wait
```

Compose starts the containers but does not register the BATCH endpoints automatically. This
matches production, where operators deploy or terminate Jupyter through the internal platform and
then update Executor through `jupyter_server_upsert`, `jupyter_server_set_state`, and
`jupyter_server_remove`.

## Scheduling contract

An eligible server must:

- have the exact requested `JupyterPool`;
- be enabled and `ACTIVE`;
- advertise the requested kernel when kernel specs are known;
- have capacity after running, waiting, and retained-retry reservations are counted.

Servers are considered in stable name order. With both BATCH servers configured at capacity one,
the first two BATCH Executions occupy different servers. A third stays `QUEUED`; the reconciliation
loop claims it after either server releases capacity. Free INTERACTIVE capacity is never used for
that queued BATCH Execution.

`/readyz` reports `jupyter_fleet=true` when any registered pool has an ACTIVE server. A BATCH-only
outage therefore does not make the whole service unready or interrupt INTERACTIVE work. Monitor the
pool metrics and alert on the pool required by each workload class.

## Scale up

1. Deploy the new Jupyter server with one of the approved kernel environments and the shared PVC.
2. Call `jupyter_server_upsert` with its stable name, endpoint, token, `pool=BATCH`, and configured
   maximum concurrency.
3. Confirm the response is `ACTIVE`, supported kernels are populated, and the BATCH server/capacity
   metrics increase.
4. Queued BATCH work is picked up automatically by PostgreSQL reconciliation.

## Drain and scale down

1. Call `jupyter_server_set_state` with `DRAINING`. New work stops immediately.
2. Wait until `drain_complete=true`; running work and retained retry kernels continue to reserve
   capacity until completed or expired.
3. Call `jupyter_server_remove` to soft-disable the registry record while preserving historical
   foreign keys.
4. Terminate the Jupyter deployment through the internal platform.

Do not terminate the platform deployment first unless accepting an infrastructure failure for its
active Executions.

## Metrics

- `executor_jupyter_pool_servers{pool,status}`: enabled registry records
- `executor_jupyter_pool_capacity{pool}`: total configured capacity on enabled ACTIVE servers
- `executor_jupyter_pool_capacity_used{pool}`: running/waiting Attempts plus retained kernels
- `executor_jupyter_pool_queued_executions{pool}`: queued requests for the pool

Metrics refresh at registry startup, server-list requests, and every health-monitor interval.
During draining or outages, usage can be greater than schedulable capacity because existing work
is preserved while the server is excluded from new scheduling.

## Local E2E

The smoke test registers the two BATCH endpoints, submits one INTERACTIVE and three BATCH
Executions, observes two running plus one queued BATCH job, and verifies that all jobs succeed on
the correct pool:

```bash
EXECUTION_WORKER_CONCURRENCY=4 uv run executor-service
uv run python scripts/jupyter_batch_pool_smoke.py
```

The future Workflow batch service should submit successful promoted plans with
`trigger_type=BATCH`, `jupyter_pool=BATCH`, and static execution mode. Workflow scheduling and
report generation remain outside this Executor repository.
