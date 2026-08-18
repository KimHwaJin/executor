# Architecture decisions

## ADR-002: Runtime-owned execution storage

- Status: ACCEPTED
- Recorded on: 2026-08-13

Agent and Executor share an input volume used only for PATH ExecutionSpec files. Runtime workspaces,
notebooks, generated datasets, artifacts, and checkpoints belong to Runtime storage. Executor never
opens a Runtime path locally; it uses Runtime Driver operations and stores only relative paths,
metadata, lineage, and checksums in PostgreSQL.

All Jupyter targets must mount the same shared Jupyter volume. This is an operator deployment
contract; Executor does not discover or compare PV/PVC identities. A retained in-memory retry stays
on its original target/session, while storage-only reads may use another healthy compatible target.

```text
Agent + Executor input volume
└── requests/.../execution-spec.json

Jupyter shared volume
└── users/{user|unscoped}/projects/{project|unscoped}/sessions/{session|unscoped}/executions/{execution}/
    ├── notebooks/execution.ipynb
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

Every Step success event carries its bounded transport-neutral Runtime result. For Jupyter this is
the cell MIME output and execution count. After all Step events, Executor publishes the Operation
outcome and `execution.waiting_for_operation`; the Agent checkpoints the results and resumes its
graph. Notebook APIs remain available for complete audit and deep inspection, but the notebook is
not the orchestration protocol.

```text
Agent -> Executor: submit Execution + initial Operation
Executor -> Agent: execution_id + operation_id + step_id receipts
Executor Worker -> Runtime: execute accepted Steps
Executor -> PostgreSQL: persist Step/Operation result
Executor -> executor.events: Step results, Operation outcome, waiting notification
Agent consumer -> LangGraph: deduplicate, checkpoint, resume
Agent -> Executor: append next Operation or finalize
```

Large-result offloading, authentication/authorization, and deterministic MULTI replay onto a new
Runtime remain deferred decisions.
