# Architecture decisions

## ADR-006: Pluggable Runtime types

- Status: ACCEPTED
- Recorded on: 2026-10-22

Jupyter is the only Runtime today, but other execution environments, such as per-run containers
or sandboxes, must be addable without changing the Worker, the API or the persistence model.

Everything Executor knows about one Runtime type lives in its adapter in
`infrastructure/runtime_adapters.py`, registered in `ADAPTER_TYPES`. An adapter provides:

- `normalize_connection_config()`: validates a Target's `connection_config`; the API contract and
  the registry both call it, so there is one rule per type.
- `create_driver()`: builds the `RuntimeDriver` for a stored Target from its config and credential.
- `allowed_profiles()`: the profiles callers may request.
- `capabilities`: a `RuntimeCapabilities` declaration.
- `artifact_uri_scheme`: the scheme of URIs recorded for Artifacts on its storage.

No code outside that module branches on `RuntimeType`. Adding a type means a `RuntimeType` value,
a driver and an adapter; Targets, Executions and Attempts already store `runtime_type`, and
selection and claiming only match the same type.

### Driver contract

Every driver implements the `RuntimeDriver` protocol in `domain/runtime.py`:

- Sessions: `start_session(profile, working_directory)`, `interrupt_session`, `abort_session`,
  `delete_session`, `session_exists`. A session may be a kernel, a container or a sandbox.
- Execution: `start_execution(session, request_id, code)` runs code at most once per request ID;
  `follow_execution` delivers its outputs in order and then the result; `release_execution`
  drops what the Runtime kept for it.
- Health: `status`, `supported_profiles`, `resource_status`.
- Workspace files while the session lives: `prepare_workspace`, `artifact_snapshot`,
  `file_metadata`, `read_manifest`, `read_notebook`, `write_notebook`, `write_text`, and
  `open_file` for downloads.

### Capabilities

Executor checks capabilities, never the type, before relying on more than the core contract:

| Capability | Meaning | Without it |
|---|---|---|
| `stateful_sessions` | Session state survives between Operations | MULTI submissions are rejected with `UNSUPPORTED_RUNTIME_CAPABILITY` (422) |
| `execution_journal` | Another process can `follow_execution` a started request from the start | A Worker that stops takes the normal failure and retry path instead of handing the Step over |
| `durable_workspace` | Workspace files outlive the session and are shared by every Target of the type | Not yet supported: notebooks and Artifacts would have to move to Executor storage before the session is released |

Jupyter declares all three, so its behaviour is unchanged. The `durable_workspace = False` path
is deliberately left for the first Runtime that needs it, where it can be verified end to end.

## ADR-005: Remove Executor-owned tracing

- Status: DONE
- Recorded on: 2026-09-04

Executor keeps standard logs (`logger.yml`), durable execution diagnostics, business IDs and public
event sequencing, and does not create spans, propagate trace context or export to OTLP/Phoenix.
The MCP SDK still pulls in `opentelemetry-api` transitively; Executor configures no tracer
provider, exporter or span processor.

Migration `0004` dropped the `traceparent`/`tracestate` columns of `executions`,
`execution_events` and `outbox_events` together with their values. Business history was kept.
`alembic downgrade 0003` restores the nullable columns but not the discarded values.

## ADR-004: PostgreSQL work admission and cluster-wide background passes

- Status: ACCEPTED
- Recorded on: 2026-10-21

Workers find work only by polling PostgreSQL; Redis carries outbound events and nothing else.
Every second each Worker dispatches the `CANCEL_REQUESTED` Executions it may claim (not those
under another Worker's live lease), then released Steps and `QUEUED`/`FINALIZING` work only up to
its free slots (`EXECUTION_WORKER_MAX_ACTIVE`). A rotating `(created_at, id)` cursor keeps one blocked Execution
from starving the rest, and the bounded claim spreads a backlog across replicas. Claims use
`FOR UPDATE SKIP LOCKED` and fencing tokens, so overlapping polls are safe.

This replaced the former `executor.work` Stream, its consumer group, DLQ and Outbox `WORK` rows.
A wake-up queue duplicated state PostgreSQL already held, and needed its own retention, DLQ and
reconciliation. Polling adds a bounded, indexed query per Worker per second and at most one
second of admission latency, which is acceptable for notebook-scale work.

Maintenance passes (event retention, Runtime health probes, lease recovery, retained-session
cleanup, MULTI audit, maintenance runs) run on one Pod per interval. `claim_pass()` in
`infrastructure/cluster_passes.py` takes a per-key lease row with a conditional `UPDATE`. Every
pass stays safe to overlap; the lease only throttles, so a Pod dying mid-pass delays the next
run by at most one interval.

Execution status changes are validated against one table, `EXECUTION_TRANSITIONS` in
`domain/transitions.py`. Domain methods and every Worker status write call
`require_execution_transition()`, so a new path cannot invent a transition silently.

## ADR-003: Decompose the Execution Worker by responsibility

- Status: ACCEPTED
- Recorded on: 2026-08-30

`ExecutionWorker` remains the process-lifecycle facade, but work admission,
job dispatch, durable claiming, Runtime execution, Step result handling,
notebook projection, cancellation, recovery, and event persistence are separate
collaborators below `infrastructure/execution_worker/`. The split is a
behavior-preserving refactor: REST and MCP contracts, PostgreSQL schema, Redis
message schemas, state transitions, workspace paths, and Runtime Driver ports do
not change.

The Worker package follows these dependency rules:

- `worker.py` owns lifecycle and coordinates collaborators; collaborators never
  import the facade.
- Work admission (`work_admission.py`) only selects work and hands it to the
  dispatcher; it never executes Runtime work directly.
- The SINGLE and MULTI runners share Step and Runtime-session handling through
  `StepFlow` (`step_flow.py`) instead of duplicating it.
- Runtime execution orchestration uses the existing domain Runtime ports and
  shared-result port. Jupyter-specific behavior remains in the Jupyter Driver.
- Execution event persistence is isolated from Runtime execution and preserves
  the existing per-Execution sequence and Transactional Outbox transaction.
- Cancellation and recovery are explicit processors, not alternate branches
  hidden inside the facade.
- New mixins, compatibility proxy methods, and generic `utils.py` or `common.py`
  modules are not used to disguise coupling.

Tests target each collaborator through its public method. Facade tests cover
only lifecycle, dispatch coordination, and composition. The old flat
`infrastructure/worker.py` module is removed after all internal imports migrate;
no compatibility shim is retained because Executor has not published that
Python import path as a supported external API.

Layer direction remains `domain <- application <- infrastructure` with inbound
`interfaces` calling application ports. `container.py` is the composition root.
An architecture test enforces that domain and application code do not import
concrete infrastructure or interface adapters.

## ADR-002: Split execution storage ownership

- Status: ACCEPTED
- Recorded on: 2026-08-13

Agent and Executor share a volume for PATH request files, immutable executed source snapshots, and
complete Step output bodies. Runtime workspaces, notebooks, generated datasets, reports, and other
artifacts belong to Runtime storage. Executor never opens a Runtime path locally; it uses Runtime
Driver operations. PostgreSQL stores authoritative state, fencing, bounded summaries, lineage,
and canonical relative result references, but not output bodies.

All Jupyter targets must mount the same shared Jupyter volume. This is an operator deployment
contract; Executor does not discover or compare PV/PVC identities. A retained in-memory retry stays
on its original target/session, while storage-only reads may use another healthy compatible target.

Execution notebooks are a retryable Runtime projection. The Jupyter extension writes accepted
Step sources as stable code cells before Kernel execution. Executor streams IOPub output into a
fenced partial directory on the shared Agent/Executor volume, atomically seals it, commits the
canonical reference under its active lease, and projects sealed results into those notebook cells.
NbModelClient, YDoc, and RTC are not runtime dependencies; live synchronization
with an already open JupyterLab document is deferred unless collaborative
editing becomes an explicit requirement. Notebook projection failure does not reverse a successful
Step; its separate status and retries remain observable.

```text
Agent + Executor shared volume
├── <agent-selected-directory>/.../step-N.py
└── executions/<execution-id>/
    ├── sources/<step-id>/source.py
    └── operations/<operation-id>/steps/<step-id>/attempts/<attempt-id>/<fence>/
        ├── outputs/...
        └── manifest.json

Jupyter shared volume
└── users/{user|unscoped}/projects/{project|unscoped}/sessions/{session|unscoped}/executions/{execution}/
    ├── notebooks/execution.ipynb
    ├── reports/final-report.md
    ├── artifacts/{datasets,plots,models,metrics,reports,logs,other}/
    └── checkpoints/
```

## ADR-001: Asynchronous Operation boundary

- Status: ACCEPTED
- Recorded on: 2026-08-13
- Updated for API contract v2: 2026-08-19

Agent owns Task, ExecutionPlan, PlanStep, LangGraph checkpoint, and replanning state. Executor owns
Execution, Operation, Step, Attempt, Step Attempt, Runtime session, outputs, notebook, artifacts,
and Outbox state. The services do not share tables or plan identifiers.

One Agent decision becomes one Executor Operation containing one or more ordered Steps. Executor
generates `execution_id`, `operation_id`, and `step_id`; the Agent stores those IDs beside its own
plan objects after submit. This explicit binding avoids ambiguous names such as OperationPlan and
PlanStep inside Executor.

`lifecycle.operation_mode` controls orchestration:

- SINGLE executes the initial Operation and reaches a terminal state. Explicit retry requeues that
  same Operation and creates a new Attempt.
- MULTI executes all Steps in the current Operation on one retained Runtime session and enters
  `WAITING_FOR_OPERATION`. The Agent may append another Operation or request finalization. Already
  executed Steps are immutable.

Both REST and MCP expose the same application commands. Submit and Operation creation return
Executor-generated ID receipts immediately. PostgreSQL commits state and event Outbox records in
one transaction. Workers find work by polling PostgreSQL; `executor.events` wakes Agent/frontend
consumers.

`execution.step_completed` carries only a bounded output summary and a structured shared-volume
result reference. Full text and image payloads stay in immutable shared files and never enter
PostgreSQL or Redis. After all Step events, Executor publishes
`execution.operation_completed`; its `continuation` tells a MULTI Agent whether another Operation
may be submitted. `execution.completed` is the terminal boundary. The Agent can call
`execution_operation_result_get` or `execution_result_get` for authoritative reconciliation.
Notebook APIs remain available for audit and deep inspection, but the notebook and Redis payload
are not the orchestration result store.

```text
Agent -> Executor: submit Execution + initial Operation
Executor -> Agent: execution_id + operation_id + step_id receipts
Executor Worker -> Runtime: execute accepted Steps
Executor -> PostgreSQL: persist Step/Operation result
Executor -> executor.events: result references and Operation/Execution boundaries
Agent consumer -> LangGraph: deduplicate, resume, fetch one consolidated result
Agent -> Executor: append next Operation or finalize
```

Automatic intent discovery for Runtime-produced Artifacts, authentication/authorization, and
deterministic MULTI replay onto a new Runtime remain deferred decisions.
