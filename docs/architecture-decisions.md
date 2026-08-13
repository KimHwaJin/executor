# Architecture Decisions

This document records cross-service decisions that affect both Executor and its upstream Agent
orchestrator. `PROPOSED` entries are discussion baselines, not implementation authorization.
Change an entry to `ACCEPTED` only after the owning teams approve its open questions. Allowed
statuses are `PROPOSED`, `ACCEPTED`, `SUPERSEDED`, and `REJECTED`.

## ADR-001: Asynchronous DYNAMIC execution boundary

- Status: ACCEPTED
- Recorded on: 2026-08-13
- Owners: Agent/API team and Executor team
- Scope: multi-Step DYNAMIC execution, Agent resume, result delivery, Notebook access

### Context

A DYNAMIC execution may run one or more consecutive Steps before the Agent needs to inspect their
results and revise the remaining plan. Individual Steps can take days. Coupling the LLM directly to
Redis payloads, Notebook parsing, callback retries, or mandatory result-discovery Tool selection
would spread orchestration state across both services and make recovery nondeterministic.

### Proposed decision

1. Treat one Agent decision as an explicit multi-Step execution boundary named
   `ExecutionOperation`. Both initial DYNAMIC submission and later continuation may contain one or more
   consecutive Steps.
2. Executor owns Execution, Operation, Step, Attempt, Step Attempt, Runtime session, execution result,
   Notebook, Artifact, and durable Outbox state. Agent owns Task, ExecutionPlan, PlanStep, graph
   checkpoint, and replanning state. The services do not share database tables.
3. Executor executes every Step in an Operation sequentially on the retained Runtime session. If a Step
   fails, later Steps in that Operation do not execute and remain durable evidence with an explicit
   terminal disposition such as `SKIPPED`.
4. Executor persists a transport-neutral structured Operation result before publishing a terminal Operation
   event. PostgreSQL is the state source of truth; PV/S3 stores large outputs and Artifacts.
5. Redis Streams is a durable notification channel. A terminal event carries stable identifiers,
   sequence range, status, failed sequence when applicable, and Execution version. It does not
   carry full cell outputs, datasets, models, images, or Notebook contents.
6. The Agent service's deterministic integration layer consumes and deduplicates the event,
   retrieves the structured result, stores it in the LangGraph checkpoint, and resumes the graph.
   The LLM interprets the prepared result and chooses the next Turn or finish; it is not responsible
   for noticing events, selecting the mandatory result-read operation, or implementing retries.
7. Notebook is an execution artifact for audit, reproducibility, download, reporting, and optional
   deep inspection. It is not the orchestration protocol or the only result source. The structured
   result contract must remain independent of Jupyter so another Runtime type can implement it.
8. REST and MCP may expose the same application query. The mandatory service-to-service resume
   path is deterministic; MCP remains suitable for optional LLM exploration and operator tooling.

### Intended interaction

```text
Agent orchestrator -> Executor: submit ExecutionOperation
Executor -> Agent orchestrator: execution_id + operation_id immediately
Executor -> Runtime: execute consecutive Steps
Executor -> PostgreSQL/PV/S3: commit Operation result and evidence
Executor -> Redis Streams: publish terminal Operation event with IDs
Agent event consumer -> Executor query: load structured Operation result
Agent event consumer -> LangGraph: checkpoint result and resume
LangGraph/LLM -> Agent orchestrator: submit next Operation or finish
```

### Deferred decisions

The following details remain open and must not be inferred from this proposal:

- REST versus MCP transport for the deterministic Agent integration client;
- exact result output-size limits, truncation, cursor, and external-output reference contract;
- terminal event names and payload schema;
- whether Operation result later becomes a separate materialized projection rather than the
  current Operation, Step, Step Attempt, and Artifact query model;
- authentication and service-to-service authorization;
- retention policy for detailed outputs and Notebooks.

### Accepted implementation boundary

- REST and MCP share one Execution submit contract selected by `mode`.
- STATIC normally has one Operation; DYNAMIC may append multiple Operations with `continue`.
- The `continue` and initial DYNAMIC source may contain one or more consecutive Steps.
- PostgreSQL stores Operation provenance and result state before the Outbox event is committed.
- Agent resumes on `execution.operation_succeeded` or `execution.operation_failed` and then reads
  the Operation and its Steps through REST or MCP.
- A STATIC retry requeues the same accepted Operation and creates a new immutable Attempt. The
  Operation reflects the latest result; Attempt and Step Attempt history preserves earlier tries.
- DYNAMIC Tool failures use a new correction Operation. DYNAMIC Runtime-state loss is not retried
  until a deterministic replay or Runtime-checkpoint policy is approved.
