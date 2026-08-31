# Executor Resilience Testing

These local E2E scenarios start real Executor processes against the Docker Compose PostgreSQL,
Redis, Jupyter-owned shared storage, and the Jupyter fleet. They verify process and dependency failure behavior that unit
tests cannot cover.

## Prerequisites

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml \
  --profile multi-jupyter --profile batch-jupyter up -d --wait
uv run alembic upgrade head
```

The optional `docker-compose.test.yml` overlay gives each Jupyter container an explicit two-CPU,
4 GiB cgroup boundary and raises Executor's maximum runtime to seven days. Override
`LOCAL_TEST_JUPYTER_CPUS`, `LOCAL_TEST_JUPYTER_MEMORY`, or
`EXECUTION_MAX_RUNTIME_SECONDS` when the local machine needs different limits. Set
`LOCAL_TEST_COMPOSE_FILES` to the platform path-separated pair
`docker-compose.yml:docker-compose.test.yml` (use `;` on Windows) when the suite must stop and
restart services with the same overlay.

### Local topology preflight

```bash
uv run python scripts/local_test_preflight.py
```

The preflight validates `/healthz`, `/readyz`, `/workerz`, the current Alembic head, PostgreSQL,
Redis, required MCP Tools, and all four local Runtime Targets. It also restores the Compose-internal
Jupyter endpoints after a real-process resilience test temporarily registered host endpoints.
Set `LOCAL_TEST_EXECUTOR_TOPOLOGY=native` when Executor itself runs on the host. Disable optional
fleet requirements with `LOCAL_TEST_INCLUDE_BATCH=false` or
`LOCAL_TEST_INCLUDE_SECONDARY=false`.

The scripts select free loopback ports automatically. Set `DRAIN_SMOKE_PRIMARY_PORT`,
`DRAIN_SMOKE_SECONDARY_PORT`, `LOAD_SMOKE_PRIMARY_PORT`, `LOAD_SMOKE_SECONDARY_PORT`,
`REDIS_OUTAGE_SMOKE_PORT`, or `JUPYTER_OUTAGE_SMOKE_PORT` only when fixed ports are required.

The SINGLE/MULTI lifecycle scenarios below use the already running Compose Executor. The
real-process scenarios start their own one or two Executor processes and therefore require
exclusive control of the shared PostgreSQL work queue:

```bash
docker compose stop executor
uv run python scripts/multi_executor_load_smoke.py
# Run the other real-process scenarios while the Compose Executor remains stopped.
docker compose up -d --wait executor
```

This applies to `multi_executor_load_smoke.py`, `multi_executor_drain_smoke.py`,
`multi_executor_failover_smoke.py`, `executor_redis_outage_smoke.py`, and
`jupyter_server_outage_smoke.py`. A unique Redis Stream does not isolate these tests because every
Executor also reconciles unleased `QUEUED` rows directly from PostgreSQL. Each script now fails
fast when the default Executor at `RESILIENCE_EXISTING_EXECUTOR_URL` is still running. Set
`RESILIENCE_ALLOW_CONCURRENT_EXECUTOR=true` only when the other Executor uses a different database.

## Automated scenarios

### SINGLE execution observability

```bash
uv run python scripts/single_execution_observability_smoke.py
```

This non-disruptive scenario uses the already running Executor stack. It submits one two-Step
SINGLE execution through REST and one through MCP, then cross-checks the public history APIs,
PostgreSQL, Redis Stream, Runtime-owned files, and Runtime Target probe. Each execution must:

- expose `QUEUED -> RUNNING -> SUCCEEDED`;
- persist two successful current Steps, one successful Attempt, and two immutable Step Attempts;
- read current Steps, Attempt detail, and Step Attempt history through their separate paginated
  REST/MCP operations;
- publish every PostgreSQL Outbox row and expose the same `event_id` values in Redis;
- register the generated text Artifact and final `execution.ipynb`;
- retain the historical Runtime session ID on the Attempt while clearing it from the terminal
  Execution; and
- leave no active execution or Runtime session after both cases finish.

The default profile is `basic`. Override `OBSERVABILITY_RUNTIME_PROFILE`,
`OBSERVABILITY_TIMEOUT_SECONDS`, or `OBSERVABILITY_STREAM_SCAN_LIMIT` when the local topology
requires it. Run this against an otherwise idle local Runtime Target because the final session
leak assertion expects the probed target to have no unrelated sessions.

### SINGLE failure, retained-session retry, and running cancellation

```bash
uv run python scripts/single_failure_retry_cancel_e2e.py
```

This non-disruptive scenario covers both abnormal SINGLE lifecycle paths against the running
Compose stack. Failure and retry use MCP; cancellation uses REST. For each Execution it
cross-checks the public current/history responses, PostgreSQL rows, Transactional Outbox, Redis
Stream event IDs and v2 payloads, shared-PV Artifact evidence, and the exact Jupyter session.

The retry case fails after writing an Artifact, verifies `FROM_FAILED_STEP` preserves the original
target and kernel, then resumes from the failed Step and deletes that kernel after success. The
failed write remains `INCOMPLETE`, the successful retry write is `AVAILABLE`, Attempt/Step Attempt
history stays immutable, and the completed notebook is registered.

The cancellation case writes a marker before entering a long cell, requests cancellation while
the kernel is running, and requires Execution, Attempt, current Steps, and the running Step Attempt
to become `CANCELLED`. The kernel must be deleted, the marker must be preserved as `INCOMPLETE`,
later Steps must not run, and no successful notebook Artifact may be registered.

Override `SINGLE_LIFECYCLE_RUNTIME_PROFILE`, `SINGLE_LIFECYCLE_TIMEOUT_SECONDS`, or
`SINGLE_LIFECYCLE_STREAM_SCAN_LIMIT` when the local topology requires it. The host-side session
probe defaults to `http://127.0.0.1:8888`; set `SINGLE_LIFECYCLE_JUPYTER_ENDPOINT` when the registered
Target needs a different host-accessible endpoint than its container-internal address.

### MULTI correction, finalization, and running cancellation

```bash
uv run python scripts/multi_execution_lifecycle_e2e.py
```

This non-disruptive scenario alternates REST and MCP commands while checking PostgreSQL, Outbox,
Redis Streams, shared-PV Artifacts, the generated notebook, and the exact Jupyter session. Its
normal flow submits one MULTI Operation, appends a successful Operation, appends an expected failure,
adds a corrected follow-up cell, and finishes. Every cell must use the same target, kernel, and
Attempt; the failed cell remains immutable rather than being rerun or replaced.

The second flow cancels a running MULTI Step after it writes a marker. The interrupted execution
job preserves the marker as an `INCOMPLETE` Artifact, while the replacement cancellation job is
the single owner of kernel cleanup and the `CANCELLED` state/event. This prevents competing cleanup
operations from incorrectly reporting a successfully removed kernel as a cleanup failure.

Override `MULTI_LIFECYCLE_RUNTIME_PROFILE`, `MULTI_LIFECYCLE_TIMEOUT_SECONDS`, or
`MULTI_LIFECYCLE_STREAM_SCAN_LIMIT` as needed. Set
`MULTI_LIFECYCLE_JUPYTER_ENDPOINT` when the host-side session probe cannot use the target's
container-internal endpoint.

### Graceful drain and handoff

```bash
uv run python scripts/multi_executor_drain_smoke.py
```

The scenario starts two Executor processes and two one-capacity INTERACTIVE Jupyter servers. The
primary receives a short execution, a long execution, and one queued execution before SIGTERM.
It verifies that:

- the short execution succeeds within the drain window;
- the long execution exceeds the window and becomes `FAILED/WORKER_SHUTDOWN/FROM_START`;
- the long execution kernel cleanup succeeds;
- the queued execution is claimed exactly once by the secondary Executor.

### Concurrent load and capacity

```bash
uv run python scripts/multi_executor_load_smoke.py
```

The default run submits 30 SINGLE executions across the INTERACTIVE and BATCH pools, with two
Executor processes and four one-capacity Jupyter servers. It requires all executions to succeed
with exactly one Attempt, both Executor consumers to own work, every server peak to remain within
capacity, INTERACTIVE/BATCH assignments to remain isolated to their requested pools, and zero
active kernels after completion.

Use `RESILIENCE_EXECUTION_COUNT` for 20-60 executions and
`RESILIENCE_CELL_SLEEP_SECONDS` to adjust overlap duration.

### Mixed output load

```bash
uv run python scripts/mixed_output_load_smoke.py
```

This submits 14 executions by default across two one-capacity INTERACTIVE targets and alternates
the `basic` and `ml` profiles. Workloads cover bounded long text, HTML tables, JSON MIME output,
PNG output, a Runtime Artifact, CPU work, and temporary memory allocation. The scenario fetches
each consolidated `execution_result_get` exactly once, validates MIME payloads and PNG integrity,
records submit/queue/total latency percentiles, enforces Runtime capacity, and requires zero leaked
reservations or kernels. Use `MIXED_LOAD_EXECUTION_COUNT` for 7-30 executions.

### T35 large-output measurement

Run the bounded smoke preset first:

```bash
uv run python scripts/t35_output_measurement.py
```

It measures one 1 MiB non-compressible text execution and one 1 MiB PNG execution. The report is
checkpointed under
`test-results/t35-output-measurement-<run-id>.json` and includes Executor RSS, Runtime memory,
PostgreSQL growth, notebook size, and consolidated result retrieval size and latency.

The complete evidence matrix is intentionally expensive:

```bash
uv run python scripts/t35_output_measurement.py --preset full --confirm-full
```

It covers non-compressible text sizes 1, 5, 10, 25, 50, and 100 MiB; PNG sizes 1, 10, 25, and
50 MiB; and active concurrency 1, 5, 10, and 20. The payloads avoid making PostgreSQL growth look
artificially small through repeated-character compression. The script does not change Runtime
Target capacity. Configure an isolated local fleet with enough aggregate capacity before the run.
By default it fails if the requested concurrency exceeds that capacity;
`--allow-queued-concurrency` explicitly changes the meaning to queued-submission load. Restrict
targets with comma-separated `T35_RUNTIME_TARGET_IDS`.

Executor process RSS is read from `/proc/1/status` through Docker exec and the script
auto-discovers the Compose `executor` service. Set
`T35_EXECUTOR_CONTAINER` for another local container, or use `--allow-missing-executor-rss` only
when an external sampler provides equivalent evidence. A non-loopback Executor is rejected unless
`T35_ALLOW_REMOTE=true` is set for an isolated non-production test stack. Override the host-side
database connection with `T35_DATABASE_URL` when needed.

The Executor rejects one oversized Runtime WebSocket message before JSON or base64 decoding. Test
that boundary separately from the successful measurement matrix by starting the isolated Executor
with a deliberately low value, then requiring the explicit failure:

```bash
# Executor container environment for this isolated run
RUNTIME_MAX_OUTPUT_MESSAGE_BYTES=1048576

uv run python scripts/t35_output_measurement.py \
  --scenario IMAGE:2:1 \
  --expect-output-limit

uv run python scripts/t35_output_measurement.py \
  --scenario IMAGE:2:1 \
  --operation-mode MULTI \
  --expect-output-limit
```

The report records the effective limit read from the Executor container and requires every
Execution to terminate with `OUTPUT_LIMIT_EXCEEDED`. It is a failure if the workload succeeds or
fails for another reason. The corresponding Step result reference and sealed manifest retain any
already committed representations with `complete=false`; no silent truncation is accepted. Use an
IMAGE scenario for the boundary test because one Jupyter display message contains the complete
base64 representation, while stream text may be split into multiple protocol messages.
The MULTI case must first reach `WAITING_FOR_OPERATION` so a corrected Operation could be added.
The harness then cancels it and requires `CANCELLED`. For SINGLE, the harness requests the
documented retry and immediately cancels it. Both cleanup paths release the retained Runtime
session after recording the measured failure evidence.

Choose the production value only after running the successful matrix with candidate limits and
comparing peak Executor RSS against the Pod memory limit at the intended active concurrency. Keep
headroom for JSON parsing, base64 expansion, Python object overhead, ordinary service memory, and
multiple Workers receiving messages concurrently.

### Long-running Jupyter soak

```bash
SOAK_DURATION_SECONDS=300 \
SOAK_OUTPUT_INTERVAL_SECONDS=60 \
  uv run python scripts/jupyter_long_running_soak.py
```

The soak submits one bounded SINGLE Step, emits low-frequency application heartbeats, records
Execution and Attempt lease samples, and validates the consolidated result, Runtime-owned
Notebook, JSON log Artifact, published Outbox events, Redis Stream delivery, terminal-event
uniqueness, reservation release, and kernel cleanup. Step, Operation, and client deadlines are
derived from the requested duration with safety margins. Recommended gates are five minutes,
30 minutes, two hours, and then 24 hours. On macOS, wrap overnight runs with `caffeinate -i`.

Reports are written under ignored `test-results/` paths. Change the directory with
`LOCAL_TEST_RESULTS_DIR`.

### Local validation suite

Quick correctness, mixed output, and five-minute soak:

```bash
uv run python scripts/local_validation_suite.py
```

Add lifecycle and 30-execution load scenarios:

```bash
LOCAL_TEST_COMPOSE_FILES="docker-compose.yml:docker-compose.test.yml" \
  uv run python scripts/local_validation_suite.py --full
```

Fault injection is explicit because it stops local Executor, Redis, or Jupyter processes:

```bash
LOCAL_TEST_COMPOSE_FILES="docker-compose.yml:docker-compose.test.yml" \
  uv run python scripts/local_validation_suite.py --full --include-faults
```

The suite captures one log per phase plus `summary.json`. It always restarts the Compose Executor
and restores Compose-internal Runtime Target endpoints after real-process scenarios, including when
a phase fails.

### Redis pause and Outbox recovery

```bash
uv run python scripts/executor_redis_outage_smoke.py
```

This uses Redis `CLIENT PAUSE ... ALL` for eight seconds. The command affects the local Redis
instance, so do not run it against a shared or production Redis. The submitted execution must
finish while Redis is paused through PostgreSQL reconciliation. After Redis resumes, every durable
integration Outbox event must become `PUBLISHED` and have the same `event_id` in the configured
event Stream. The internal `operation.ready` work message is verified separately in the work
Stream.

Set `REDIS_OUTAGE_PAUSE_MILLISECONDS` to a value of at least 5000 when a longer pause is needed.

### Jupyter OFFLINE and recovery

```bash
ALLOW_DOCKER_JUPYTER_OUTAGE_TEST=1 \
  uv run python scripts/jupyter_server_outage_smoke.py
```

The explicit opt-in is required because the script temporarily stops the local
`jupyter-secondary` container. It always brings the container back in `finally`. The test probes
the stopped server as `OFFLINE`, verifies that work uses the healthy primary, restores and probes
the secondary as `ACTIVE`, and then verifies that both servers receive concurrent work.

For a retained Tool-error kernel, run:

```bash
uv run python scripts/jupyter_retry_offline_recovery_smoke.py
```

This test makes the registered endpoint temporarily unreachable without deleting the live kernel,
requests retry, verifies the Execution remains `QUEUED` on the original server and kernel, restores
the endpoint, and confirms execution resumes from the failed Step.

### Forced process loss

```bash
uv run python scripts/multi_executor_failover_smoke.py
```

This existing scenario sends SIGKILL to the primary Executor. The surviving process must classify
the expired lease as `LEASE_EXPIRED`, delete the abandoned kernel, and complete exactly one
explicit `FROM_START` retry.

### Isolated Docker Worker loss

This is the repeatable application-level acceptance test for Worker crash recovery. It creates a
dedicated Compose project with its own PostgreSQL, Redis, Jupyter, shared volumes, and two Executor
containers. It does not reuse or remove the ordinary local Compose project.

```bash
uv run python scripts/docker_worker_failover_e2e.py \
  --allow-container-kill
```

The command builds `executor-service:failover` by overlaying the current application and migration
sources on the existing `executor-service:local` dependency image. It reuses
`executor-jupyter:local` by default, so ordinary source changes need no external registry lookup.
If dependencies or the main Dockerfile changed, rebuild or load `executor-service:local` first. If
the Jupyter harness changed, pass `--build-jupyter-image`; in a closed network, pre-load all base
images from the internal registry before doing so.

The validator registers the isolated Jupyter Target, submits a long SINGLE Execution, identifies
its owning Worker from the Attempt lease, sends `SIGKILL` to only that container, and uses the
surviving Executor API to verify recovery and retry. By default it removes only its uniquely named
Compose project and volumes when the run ends. Use `--keep-stack` only when failed containers,
logs, PostgreSQL, or result files must be retained for diagnosis.

The default host ports are `8010` and `8011`. Override them when occupied:

```bash
uv run python scripts/docker_worker_failover_e2e.py \
  --primary-port 18010 \
  --secondary-port 18011 \
  --allow-container-kill
```

The report is written to `test-results/docker-worker-failover.json`. This Docker gate verifies the
Executor application invariants listed in the Kubernetes scenario below. It does not verify
Kubernetes scheduling, Downward API identity injection, Probes, Service/Istio routing, or PVC
mount behavior.

The isolated Docker baseline validated on 2026-08-26 force-killed the Secondary owner with exit
code 137. The Primary fenced the lease as `LEASE_EXPIRED`, completed abandoned session cleanup,
accepted one `FROM_START` retry on a new Runtime session, and reached `SUCCEEDED`. PostgreSQL
returned ten unique durable events with contiguous `event_sequence` values from 1 through 10. The
generated Compose project and its four dedicated volumes were removed automatically.

### Kubernetes Worker Pod loss

This optional platform-level scenario is retained for initial cluster qualification and major
deployment changes. Run it only in an isolated non-production namespace. It submits a long SINGLE
Execution, finds the owning Pod from the immutable Attempt lease, force deletes that exact Pod,
and verifies lease fencing, abandoned Runtime cleanup, explicit retry, event ordering, and final
success.

Prerequisites:

- the release migration Job completed and `alembic_version.version_num` is `0001`;
- the Executor Deployment and at least one compatible Runtime Target are Ready;
- the active kubectl identity can get Deployments and Pods and delete the selected Executor Pod;
- `--base-url` reaches the same Deployment selected by `--namespace`, `--deployment`, and
  `--selector`; and
- no production workload shares the namespace, Runtime pool, or Executor database.

Two or more replicas keep the Service continuously available. One replica is also supported; the
validator tolerates the temporary HTTP outage while the Deployment creates its replacement Pod.

```bash
uv run python scripts/kubernetes_worker_failover_e2e.py \
  --base-url https://executor.example.internal \
  --context non-production-cluster \
  --namespace executor-test \
  --deployment executor \
  --runtime-profile basic \
  --allow-pod-delete
```

If the Gateway requires a bearer token, inject it without placing it on the command line:

```bash
export KUBE_FAILOVER_BEARER_TOKEN='<temporary test token>'
```

An internal CA can be supplied with `--ca-file`. The script does not provide an insecure TLS
switch. The default report is written to `test-results/kubernetes-worker-failover.json`, which is
ignored by Git.

The run passes only when all of these invariants hold:

1. the first Attempt owner exactly matches the Pod that was deleted;
2. the Execution and first Attempt become `FAILED/LEASE_EXPIRED`;
3. abandoned Runtime cleanup reaches `SUCCEEDED` and the old session is released;
4. `execution_retry` creates exactly one second Attempt with a different Runtime session;
5. the retry succeeds; and
6. durable events have unique IDs, contiguous Execution-scoped sequences, and exactly one failed
   followed by one successful `execution.completed` cycle.

The validator deliberately retains the Execution, result files, and event history as audit
evidence. It does not scale the Deployment or alter maintenance admission state.

### Runtime-owned storage operation failures

```bash
uv run pytest -q tests/test_runtime_storage_failures.py
```

The focused regression suite injects failures into workspace preparation, notebook persistence,
and Artifact discovery. It verifies that Execution, Attempt, Operation, and Step state remains
consistent; the failure is classified as `RUNTIME_UNAVAILABLE/FROM_START`; Runtime sessions are
cleaned up when one was created; and the corresponding `execution.operation_completed` and
`execution.completed` Outbox events are durably
recorded. Artifact discovery state remains queryable from PostgreSQL rather than a separate event.

## Cleanup and retained evidence

Each scenario uses a unique Redis Stream and deletes it on exit. Set
`RESILIENCE_KEEP_STREAMS=true` to retain the primary and DLQ Streams for diagnosis. PostgreSQL
Execution, Attempt, Step, Artifact, and Outbox rows and execution-scoped PV files are intentionally
retained as audit evidence.

Useful read-only PostgreSQL checks are:

```sql
SELECT id, status, failure_type, retry_strategy, lease_owner,
       runtime_target_id, runtime_session_id, created_at, finished_at
FROM executions
WHERE user_id = 'resilience-user'
ORDER BY created_at DESC;

SELECT execution_id, attempt_number, status, lease_owner,
       runtime_target_id, runtime_session_id, failure_type, runtime_session_cleanup_status
FROM execution_attempts
WHERE execution_id IN (
    SELECT id FROM executions
    WHERE user_id = 'resilience-user'
)
ORDER BY started_at DESC;

SELECT aggregate_id AS execution_id, event_type, status,
       attempt_count, created_at, published_at, last_error
FROM outbox_events
WHERE aggregate_id IN (
    SELECT id FROM executions
    WHERE user_id = 'resilience-user'
)
ORDER BY created_at DESC;
```

For a retained Stream, inspect delivery state with:

```bash
redis-cli XINFO GROUPS <stream-name>
redis-cli XPENDING <stream-name> <consumer-group>
redis-cli XRANGE <stream-name> - +
```

## Validated baseline

Current text/PNG partial-result checks for real cancellation and cooperative
SIGTERM use an isolated Docker stack, without changing the normal local service:

```bash
uv run python scripts/docker_interrupted_result_e2e.py
```

See [Docker interruption validation](docker-interrupted-result-validation.md)
for the SINGLE/MULTI × basic/ml matrix, storage/event assertions, notebook
freshness fix, cleanup rules, and explicit limitations. The older baselines
below describe their historical contracts and are not current event schemas.

The API contract v2 regression baseline validated on 2026-08-19 produced:

- REST and MCP SINGLE success: two Steps, one immutable Attempt, matching PostgreSQL integration
  Outbox and `executor.events` IDs, Runtime-owned notebook and Artifact, and zero leaked sessions;
- SINGLE failure/retry and cancellation: retained-server/kernel `FROM_FAILED_STEP` retry,
  `INCOMPLETE` failed output evidence, successful notebook finalization, and running cancellation
  cleanup;
- MULTI lifecycle: append-only multi-Step Operations, failed Operation correction, retained Runtime
  state, explicit finalization, Step result events, and running cancellation;
- concurrent load: 30/30 successful SINGLE executions, both Executor owners used, strict
  INTERACTIVE/BATCH isolation, capacity peak one on every one-capacity Target, and zero leaked
  kernels;
- graceful drain: short work succeeded, long work became `WORKER_SHUTDOWN/FROM_START` with cleanup
  `SUCCEEDED`, and queued work moved to the secondary Executor;
- Redis pause: execution completed during the eight-second pause, then one work message and all
  seven integration events recovered with matching PostgreSQL/Redis event IDs;
- Runtime recovery: OFFLINE Target avoidance and reactivation, retained retry pinned to the same
  server and kernel, and notebook reads failed over to another Target on the shared Runtime volume;
- process loss: primary SIGKILL became `LEASE_EXPIRED/FROM_START`, cleaned the abandoned kernel,
  and completed on the secondary Executor; and
- static/unit integration checks: Ruff and ty passed, the default suite passed 131 tests with six
  opt-in PostgreSQL cases skipped, and all six PostgreSQL cases passed when explicitly enabled.

The real-process tests also verified that their Redis Stream isolation is insufficient when an
unmanaged Executor shares PostgreSQL. The exclusive-worker preflight is part of this baseline.

The local baseline validated on 2026-08-13 produced:

- SINGLE and MULTI lifecycle: normal execution, retained-session retry, correction/append,
  finish, running cancellation, notebooks, Artifacts, DB history, and Redis events succeeded;
- graceful drain: short `SUCCEEDED`, long `WORKER_SHUTDOWN` with cleanup `SUCCEEDED`, queued work
  owned by the secondary;
- load: 30/30 `SUCCEEDED`, two distinct Executor owners, peak one execution per one-capacity
  Jupyter server, pool isolation verified, zero leaked kernels;
- Redis pause: execution completed during the pause and all five Outbox events published after
  recovery;
- Jupyter outage: `OFFLINE` avoidance succeeded, the restored server returned to scheduling, and
  retained-kernel retry stayed pinned while its server was temporarily offline;
- SIGKILL failover: the expired Execution, Attempt, and active Operation all became failed, the
  abandoned kernel cleanup succeeded, and explicit `FROM_START` retry completed on the survivor;
- Runtime storage failures: workspace preparation, notebook write, and Artifact discovery all
  produced consistent terminal DB state and durable failure events.
