# Deferred Decisions

This document is the source of truth for Executor topics intentionally postponed until the Agent
and analysis teams agree on their contracts. Search for `Status: DEFERRED` to find open items.
When a decision is resumed, update its status, decision, owner, and affected implementation before
changing code.

Allowed statuses are `DEFERRED`, `DECIDED`, `IMPLEMENTING`, and `DONE`.

## DD-001: Materializing analysis Tool return values

- Status: DEFERRED
- Area: Agent code generation, analysis Tool contract, Jupyter runtime, Executor validation
- Deferred on: 2026-08-09
- Resume when: the Agent, Executor, and analysis teams agree on who inserts persistence behavior
  and how the approved plan maps to the final notebook cells

### Context

Analysis-provided preprocessing Tools return in-memory values such as DataFrames. They do not
accept a storage path and do not contain persistence code. The analysis team can identify which
Tool return values must be stored, but the platform contract for performing that storage is not
yet agreed.

### Agreed constraints

- Whether a preprocessing result must be stored is determined by a deterministic platform policy,
  not by an unconstrained LLM decision.
- Analysis Tools should not be forced to accept an Executor Asset ID or platform-specific storage
  path solely for persistence.
- The Agent-approved plan, executed notebook cells, ExecutionSteps, and final notebook must remain
  traceable without unexplained hidden user-code changes.
- Large datasets must be written through the shared PV or object storage; they must not be returned
  through MCP, HTTP, Redis, or Jupyter WebSocket payloads.
- A required output that cannot be materialized must not be reported as a fully successful stored
  result.

### Questions still open

- Whether persistence calls are compiled into Agent-produced cells, injected by an Executor-owned
  wrapper, implemented with a Jupyter extension/hook, or handled by another agreed mechanism.
- How one logical ExecutionPlan Step maps to visible notebook cells and platform-only bootstrap or
  post-processing operations.
- The machine-readable Tool output contract, including return selectors for scalar, tuple, mapping,
  model, and DataFrame outputs.
- Which component owns serializers and their version compatibility across the two kernel
  environments.
- How persistence failures affect Step state and dynamic replanning.

### Explicitly excluded until resumed

- No `executor_runtime` package is introduced.
- Executor does not serialize arbitrary in-memory Tool returns.
- Executor does not inject persistence cells or rewrite approved cells.
- Directory discovery and Artifact Manifest ingestion remain evidence-collection mechanisms; they
  do not turn an in-memory return value into a file.

## DD-002: Reusable Asset promotion and sharing scope

- Status: DEFERRED
- Area: Agent/API Asset catalog, Executor Artifact storage, shared PV lifecycle
- Deferred on: 2026-08-09
- Depends on: DD-001
- Resume when: preprocessing storage policy and user-versus-project reuse policy are approved

### Context

Executor currently owns immutable execution evidence as `ExecutionArtifact`. The Agent/API service
is expected to own the user-facing `Asset` catalog. It is not yet decided whether processed data is
reusable across all projects owned by the same user or only within its source project.

### Agreed constraints

- Raw immutable daily source data is shared and stored in S3.
- Processed data is isolated by user and stored on the shared PV.
- Only successful results may become reusable Assets; failed or cancelled execution output is not
  promoted.
- Executor and Agent/API own separate database tables and exchange durable events rather than
  sharing tables.
- ExecutionArtifact lineage must retain the producing Execution, Attempt, Step, Tool, parent data,
  parameters, creator, timestamps, location, size, format, checksum, and status where available.
- Tools are not required to receive an Asset ID before execution.

### Questions still open

- Default catalog visibility: `USER` or `PROJECT`.
- Whether promotion is automatic from a resolved submission policy or initiated by a later explicit
  command.
- Physical promotion strategy for large files: atomic move, reflink/hard link where supported,
  copy, or stable canonical output path.
- Deduplication identity and how repeated executions link to an existing Asset.
- Cleanup interaction between execution-scoped output and promoted reusable data.

### Explicitly excluded until resumed

- No Asset promotion API or MCP Tool.
- No automatic move or copy into a reusable Asset directory.
- No user/project visibility rule in Executor.
- Agent/API Asset CRUD remains outside this repository.

## DD-003: Additional audit actor types

- Status: DEFERRED
- Area: public mutation contracts, background operations, audit attribution
- Deferred on: 2026-08-10
- Resume when: an external caller or operational requirement needs attribution beyond end users
  and scheduled batch executions

### Current decision

- Public mutation requests accept only `USER` and `BATCH` actor types.
- `actor.id` is the stable identifier supplied by the upstream Agent/API or Batch service.
- Interactive submissions require `USER`; batch submissions require `BATCH`.
- Executor-created child records inherit the actor of the user or batch command that caused them.
- Executor background maintenance that has no new external command may leave actor fields nullable
  while retaining its operational identity in logs and traces.

### Questions still open

- Whether `SYSTEM`, `SERVICE`, or `WORKER` should become first-class actor types.
- Which stable service-instance identity should be persisted for automatic health probes, recovery,
  reconciliation, and Outbox publication updates.
- Whether internal actor identities should be visible through public APIs or only audit exports.

### Explicitly excluded until resumed

- No additional actor enum values beyond `USER` and `BATCH`.
- No fabricated system user IDs for autonomous Executor maintenance.

## DD-004: REST idempotency key transport

- Status: DEFERRED
- Area: REST mutation contracts, MCP Tool contracts, client retry behavior
- Deferred on: 2026-08-13
- Resume when: the public REST API contract is finalized or an upstream client requires the
  conventional `Idempotency-Key` HTTP header

### Current decision

- Mutation callers create the idempotency key before sending a command. Executor does not issue
  the key.
- The Agent/API service creates keys for interactive commands, and the Batch service creates keys
  for batch commands.
- A retry of the same logical command reuses the same key and payload. A new command uses a new
  key; reusing a key with different command content is rejected as a conflict.
- REST and MCP currently carry `idempotency_key` in their request bodies so both transports can
  map directly to the same application Command contract.

### Deferred option

- Move REST mutation keys to the `Idempotency-Key` HTTP header while keeping
  `request.idempotency_key` as an MCP Tool argument.
- Normalize both transport forms into the existing internal `Command.idempotency_key`; persistence
  and idempotency semantics should remain transport-independent.

### Questions still open

- Whether every REST mutation or only public execution commands should use the header.
- Whether a temporary compatibility period should accept both header and body, and how conflicting
  duplicate values should be rejected.
- Whether OpenAPI examples, client helpers, and upstream retry documentation require a coordinated
  breaking-version release.

### Explicitly excluded until resumed

- No REST request field or header change.
- No Executor-generated idempotency keys.
- No change to MCP Tool input schemas or database uniqueness behavior.

## DD-005: DYNAMIC Runtime-state recovery and replay

- Status: DEFERRED
- Area: DYNAMIC execution, Runtime checkpointing, Agent replanning, infrastructure recovery
- Deferred on: 2026-08-13
- Resume when: the Agent and Executor teams approve how a replacement Runtime reconstructs prior
  in-memory state

### Current decision

- A DYNAMIC Tool error completes the current Operation as `FAILED`, returns the Execution to
  `WAITING_FOR_CONTINUE`, and accepts a new correction Operation on the retained session.
- Loss of the retained Runtime session or other infrastructure failure that makes its state
  untrustworthy is terminal and `NOT_RETRYABLE`.
- `execution_retry` is restricted to STATIC executions.

### Questions still open

- Whether recovery replays every previously successful Step, restores a Runtime-native checkpoint,
  or starts a completely new Execution.
- How to detect and prevent duplicate external side effects during replay.
- Whether replay uses the original code and inputs or a newly approved Agent plan.
- Which Artifacts and outputs from the abandoned Runtime remain authoritative.

### Explicitly excluded until resumed

- No automatic DYNAMIC replay on a replacement Runtime.
- No inference that a new kernel can continue from prior in-memory variables.
- No reuse of STATIC `FROM_START` semantics for DYNAMIC.

## Resolved observability integration: Arize Phoenix

- Status: DONE
- Runtime image for local integration tests: `arizephoenix/phoenix:nightly`
- Validated UI and OTLP/HTTP endpoint: port `6006`, collector path `/v1/traces`
- Optional OTLP/gRPC port exposed for compatibility: `4317`

Executor now connects its spans to the Agent trace through W3C trace context and exports with the
vendor-neutral OpenTelemetry OTLP/HTTP protocol. The context survives the asynchronous boundary by
being persisted on Execution and Outbox Event rows and carried in Redis Stream fields. PostgreSQL
reconciliation falls back to the Execution context. Phoenix remains optional and is not included
in `/readyz`.

The implementation records explicit bounded attributes only. Jupyter tokens, generated code
bodies, cell outputs, dataset content, database/Redis credentials, query statements, OTLP header
values, and exception messages are excluded. A local smoke test sends a complete synthetic
Agent-to-Jupyter trace and verifies the trace and span names through Phoenix's REST API.
