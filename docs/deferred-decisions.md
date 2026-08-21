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
- Large datasets must be written through Jupyter shared storage or object storage; they must not be returned
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
- Area: Agent/API Asset catalog, Executor Artifact metadata, Jupyter shared-storage lifecycle
- Deferred on: 2026-08-09
- Depends on: DD-001
- Resume when: preprocessing storage policy and user-versus-project reuse policy are approved

### Context

Executor currently owns immutable execution evidence as `ExecutionArtifact`. The Agent/API service
is expected to own the user-facing `Asset` catalog. It is not yet decided whether processed data is
reusable across all projects owned by the same user or only within its source project.

### Agreed constraints

- Raw immutable daily source data is shared and stored in S3.
- Processed data is isolated by user and stored on Jupyter shared storage.
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

- Public mutation requests accept `AGENT`, `USER`, and `BATCH` actor types.
- `actor.id` is the stable identifier supplied by the upstream Agent/API or Batch service.
- Interactive submissions accept `AGENT` or `USER`; batch submissions require `BATCH`.
- Executor-created child records inherit the actor of the Agent, user, or batch command.
- Executor background maintenance that has no new external command may leave actor fields nullable
  while retaining its operational identity in logs and traces.

### Questions still open

- Whether `SYSTEM`, `SERVICE`, or `WORKER` should become first-class actor types.
- Which stable service-instance identity should be persisted for automatic health probes, recovery,
  reconciliation, and Outbox publication updates.
- Whether internal actor identities should be visible through public APIs or only audit exports.

### Explicitly excluded until resumed

- No additional actor enum values beyond `AGENT`, `USER`, and `BATCH`.
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

## DD-005: MULTI Runtime-state recovery and replay

- Status: DEFERRED
- Area: MULTI execution, Runtime checkpointing, Agent replanning, infrastructure recovery
- Deferred on: 2026-08-13
- Resume when: the Agent and Executor teams approve how a replacement Runtime reconstructs prior
  in-memory state

### Current decision

- A MULTI Tool error completes the current Operation as `FAILED`, returns the Execution to
  `WAITING_FOR_OPERATION`, and accepts a new correction Operation on the retained session.
- Loss of the retained Runtime session or other infrastructure failure that makes its state
  untrustworthy is terminal and `NOT_RETRYABLE`.
- `execution_retry` is restricted to SINGLE executions.

### Questions still open

- Whether recovery replays every previously successful Step, restores a Runtime-native checkpoint,
  or starts a completely new Execution.
- How to detect and prevent duplicate external side effects during replay.
- Whether replay uses the original code and inputs or a newly approved Agent plan.
- Which Artifacts and outputs from the abandoned Runtime remain authoritative.

### Explicitly excluded until resumed

- No automatic MULTI replay on a replacement Runtime.
- No inference that a new kernel can continue from prior in-memory variables.
- No reuse of SINGLE `FROM_START` semantics for MULTI.

## DD-006: Declaring and discovering Step-produced Artifacts

- Status: DEFERRED
- Area: Execution Step contract, Agent planning, analysis Tool contract, Jupyter workspace,
  Artifact registration
- Deferred on: 2026-08-21
- Related to: DD-001
- Resume when: the Agent, Executor, and analysis teams agree on how an executed Step identifies
  files that must become tracked Artifacts

### Context

Agent-authored content such as a final Markdown report can be materialized explicitly through an
Artifact command using `INLINE` or Agent/Executor shared-input `PATH` content. Runtime-produced
Artifacts are different: Python executed by a Step may create plots, models, datasets, metrics, or
other files inside the Jupyter execution workspace. Executor needs a deterministic way to know
which created files are intended Artifacts and which Step produced them.

Adding `declared_artifacts` to every Step is one candidate, but it has not been accepted. Static
declarations may not represent Tools that choose filenames dynamically, return in-memory values,
or create a variable number of outputs. Blindly scanning the whole workspace after each Step is
also ambiguous and can be expensive.

### Agreed constraints

- Artifact registration must retain Execution, Attempt, Step, Tool, storage, checksum, creator,
  timestamps, and available lineage references.
- Runtime-generated files remain on Runtime-owned Jupyter storage; large content must not pass
  through HTTP, MCP, Redis, or PostgreSQL JSON fields.
- Agent callers do not choose arbitrary Jupyter target directories. Executor owns canonical
  workspace directories such as `reports/` and `artifacts/<type>/`.
- A final report may use an explicit Artifact materialization command, but that does not determine
  how arbitrary Python Step outputs are discovered.
- Missing required output and Artifact registration failure must have explicit state semantics;
  they must not be silently reported as successful stored output.
- The design must not reintroduce unexplained hidden code cells or break traceability between the
  approved Step and the final notebook.

### Candidate approaches requiring review

- Static Step declarations containing expected Artifact type, name, media type, and canonical
  relative path.
- A Runtime-written Artifact Manifest that reports the actual dynamic files created by a Step.
- A standardized analysis Tool return/output contract interpreted by an agreed Runtime adapter.
- A narrow before/after directory-delta check used only as supporting evidence, not as the sole
  Artifact intent signal.
- A hybrid in which static declarations define required outputs and a Runtime Manifest resolves
  their actual paths and metadata.

### Questions still open

- Whether Artifact intent is declared by Agent planning policy, analysis Tool metadata, or both.
- How Python code learns its canonical Artifact output location without Tool-specific platform
  parameters or hidden source rewriting.
- How dynamic filenames, multiple outputs, optional outputs, and directory-shaped datasets are
  represented.
- Whether an undeclared file may be registered and whether an unfulfilled declaration fails only
  Artifact registration, the Step, the Operation, or the whole Execution.
- When checksum, file size, schema, and row/column metadata are collected and how retries remain
  idempotent.
- Whether report materialization and Runtime-produced Artifact registration share one public
  command contract or only the same underlying Artifact model.

### Explicitly excluded until resumed

- No `declared_artifacts` field is added to the public Step contract yet.
- No whole-workspace or whole-directory scan is treated as authoritative Artifact discovery.
- Executor does not infer Artifact intent only from filename extensions or newly created files.
- No arbitrary in-memory Tool return serialization is implemented; DD-001 remains authoritative.
- No automatic promotion from ExecutionArtifact to a reusable Agent-owned Asset is introduced.

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
