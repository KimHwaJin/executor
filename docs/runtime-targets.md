# Runtime Target Operations

Executor uses two strict Runtime scheduling pools: `INTERACTIVE` for user-driven analysis and
`BATCH` for promoted Workflow runs. Pool selection is persisted on the Execution. The scheduler
never falls back across pools. Jupyter is currently the only implemented Runtime Driver; adding a
future driver does not change Execution, scheduling, Attempt, or fleet-management contracts.

## Local topology

All local Jupyter containers use the `executor-jupyter:local` image built from
`jupyter/datascience-notebook:latest`, mount the same `./notebook_dir:/workspace/pv` shared-PV
contract, and load the same Jupyter server configuration.

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
then update Executor through `runtime_target_upsert`, `runtime_target_set_state`, and
`runtime_target_remove`.

## Jupyter resource endpoint

The custom image installs `executor_resource_extension` in the Jupyter server environment. It is
enabled at image build time and exposes:

```http
GET /executor/resource-status
Authorization: token <JUPYTER_TOKEN>
```

The endpoint uses the existing Jupyter authentication and returns aggregate resource data only:

```json
{
  "schema_version": "1.0",
  "process_count": 2,
  "cpu": {
    "used_cores": 0.15,
    "capacity_cores": 2.0,
    "utilization": 0.075,
    "source": "CGROUP_V2",
    "estimated": false,
    "errors": []
  },
  "memory": {
    "used_bytes": 138850304,
    "capacity_bytes": 4294967296,
    "utilization": 0.032329,
    "source": "CGROUP_V2",
    "estimated": false,
    "errors": []
  },
  "observed_at": "2026-08-12T05:35:54.730705+00:00"
}
```

CPU is a rate calculated between consecutive requests, so `used_cores` and `utilization` are null
on the first request. The collector prefers cgroup v2. If a usage file is missing or unreadable,
it falls back independently for that resource to same-UID psutil process aggregation. A finite
cgroup capacity overrides the configured fallback values:

```env
JUPYTER_RESOURCE_CPU_CORES=2
JUPYTER_RESOURCE_MEMORY_BYTES=4294967296
```

Authentication failure returns HTTP 403. Measurement fallback does not make the Jupyter server
unhealthy; `source`, `estimated`, and safe error codes describe the result. Local Compose
healthchecks require both the standard Jupyter status endpoint and this Extension endpoint.

The endpoint is the Runtime Driver observation contract. Persisting these observations on Runtime
Targets and using them in load-aware target selection is a separate Executor scheduler change;
the current scheduler still admits work by PostgreSQL reservations and configured target slots.

## Scheduling contract

PostgreSQL reservation and Runtime Target capacity are the only execution admission controls.
Executor does not hold a process-local semaphore for the lifetime of an Execution. This lets newly
registered targets contribute capacity without restarting Executor and keeps multi-Pod behavior
consistent. If no target slot is available, the Execution remains durably `QUEUED`; reconciliation
tries it again after capacity changes. Cancellation remains available while work is queued.

An eligible target must:

- have the exact requested `RuntimePool` and `runtime_type`;
- be enabled and `ACTIVE`;
- advertise the requested `runtime_profile`, restricted by `RUNTIME_ALLOWED_PROFILES`;
- have capacity after running, waiting, and retained-retry reservations are counted.

Targets are considered in stable name order. With both BATCH targets configured at capacity one,
the first two BATCH Executions occupy different targets. A third stays `QUEUED`; the reconciliation
loop claims it after either target releases capacity. Free INTERACTIVE capacity is never used for
that queued BATCH Execution.

`/readyz` reports `runtime_fleet=true` when any registered pool has an ACTIVE target. A BATCH-only
outage therefore does not make the whole service unready or interrupt INTERACTIVE work. Use
`runtime_target_list` to inspect the status and capacity of each pool.

## Scale up

1. Deploy the new Jupyter server with one of the approved kernel environments and the shared PVC.
2. Call `runtime_target_upsert` with `runtime_type=JUPYTER`, a stable name,
   `connection_config={"endpoint": "..."}`, credential, `pool=BATCH`, and configured capacity.
3. Confirm the response is `ACTIVE`, supported profiles are populated, and
   `runtime_target_list(pool=BATCH)` includes the new target and capacity.
4. Queued BATCH work is picked up automatically by PostgreSQL reconciliation.

## Drain and scale down

1. Call `runtime_target_set_state` with `DRAINING`. New work stops immediately.
2. Wait until `drain_complete=true`; running work and retained retry sessions continue to reserve
   capacity until completed or expired.
3. Call `runtime_target_remove` to soft-disable the registry record while preserving historical
   foreign keys.
4. Terminate the Jupyter deployment through the internal platform.

Do not terminate the platform deployment first unless accepting an infrastructure failure for its
active Executions.

## Local E2E

### One native Jupyter server without Docker

When Executor and Jupyter run on the same machine, both processes must see the same absolute
Workspace directory. Configure `.env` before starting Executor:

```env
RUNTIME_ENABLED=true
RUNTIME_TARGET_NAME=single-jupyter
JUPYTER_ENDPOINT=http://127.0.0.1:8888
JUPYTER_TOKEN=change-me-local-only
RUNTIME_POOL=INTERACTIVE
RUNTIME_ALLOWED_PROFILES=python3
RUNTIME_DEFAULT_MAX_CONCURRENT_EXECUTIONS=1
WORKSPACE_HOST_ROOT=C:/absolute/path/to/executor/notebook_dir
WORKSPACE_RUNTIME_ROOT=C:/absolute/path/to/executor/notebook_dir
```

Use the equivalent absolute POSIX path on Linux or macOS. Start Jupyter with that directory as its
root so the relative kernel path sent by Executor resolves to the same Execution Workspace:

```bash
jupyter lab --no-browser --ip 127.0.0.1 --port 8888 \
  --ServerApp.root_dir="C:/absolute/path/to/executor/notebook_dir" \
  --IdentityProvider.token="change-me-local-only"
```

After PostgreSQL, Redis, Jupyter, and Executor are running, execute the self-contained smoke test:

```bash
uv run python scripts/single_jupyter_smoke.py
```

The script registers/probes the configured Jupyter Runtime Target through MCP, submits a two-Step STATIC
Execution, waits for `SUCCEEDED`, and verifies the `.ipynb` plus a generated Artifact. Override
`EXECUTOR_MCP_URL`, `SINGLE_JUPYTER_ENDPOINT`, `SINGLE_JUPYTER_TOKEN`,
`SINGLE_JUPYTER_KERNEL`, or `SINGLE_JUPYTER_TIMEOUT_SECONDS` when testing non-default endpoints.

### Two-pool Compose scenario

The smoke test saturates both BATCH Jupyter slots, verifies a subsequently submitted INTERACTIVE
Execution still completes, then observes two running plus one durably queued BATCH job. It also
verifies that all jobs succeed on the correct pool:

```bash
uv run executor-service
uv run python scripts/jupyter_batch_pool_smoke.py
```

The future Workflow batch service should submit successful promoted plans with
`trigger_type=BATCH` and static execution mode. Executor derives `runtime_pool=BATCH`; callers do
not choose a pool directly. Workflow scheduling and report generation remain outside this
Executor repository.
