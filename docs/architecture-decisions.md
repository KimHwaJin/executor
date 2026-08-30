# Architecture decisions

## ADR-003: Decompose the Execution Worker by responsibility

- Status: ACCEPTED
- Recorded on: 2026-08-30

`ExecutionWorker` remains the process-lifecycle facade, but Redis consumption,
job dispatch, durable claiming, Runtime execution, Step result handling,
notebook projection, cancellation, recovery, and event persistence are separate
collaborators below `infrastructure/execution_worker/`. The split is a
behavior-preserving refactor: REST and MCP contracts, PostgreSQL schema, Redis
message schemas, state transitions, workspace paths, and Runtime Driver ports do
not change.

The Worker package follows these dependency rules:

- `worker.py` owns lifecycle and coordinates collaborators; collaborators never
  import the facade.
- Redis transport code does not execute Runtime work directly. It validates and
  acknowledges envelopes around a supplied dispatch callback.
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
├── requests/.../step-N.py
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
Executor-generated ID receipts immediately. PostgreSQL commits state and Outbox records in one
transaction. `executor.work` wakes Workers; `executor.events` wakes Agent/frontend consumers.

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
