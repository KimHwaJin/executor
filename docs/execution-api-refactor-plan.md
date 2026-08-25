# Execution API contract refactor plan

Status: approved for implementation preparation

This document fixes the target contract for the Execution-facing REST and MCP APIs. Runtime
Target and Runtime Pool administration APIs are outside this refactor.

## Ground rules

- PostgreSQL remains authoritative for Execution state and identifiers.
- Redis events remain bounded wake-up notifications, not result bodies.
- Agent-facing result APIs return compact indexes and immutable shared-PV result references.
- Executed source and complete Step outputs remain in files declared by each result manifest.
- REST and MCP reuse the same successful Pydantic response contracts whenever transport semantics
  do not require a different envelope.
- Resource list/detail responses retain the complete audit field set. Compact objects nested below
  a Result response omit audit fields entirely.
- Existing pre-release contracts are replaced cleanly; no legacy aliases are retained.

## 1. Request limits

Add only the following configurable cardinality limits:

- `max_steps_per_operation`
- `max_steps_per_execution`

Apply the first limit to initial submit and later Operation creation. Apply the second under the
same transaction that accepts a new Operation so concurrent requests cannot exceed it.

Do not add new metadata-size, input-parameter-size, or timeout-upper-bound settings in this
refactor.

## 2. Command APIs

Keep the existing submit, cancel, retry, Operation-create, and finalize request/response shapes.
The command receipt continues to return Executor-owned Execution, Operation, and Step IDs, current
state/version, and the complete audit fields.

For `POST /api/v1/executions/{execution_id}/operations`, change `Location` to the newly created
Operation detail URI:

```text
/api/v1/executions/{execution_id}/operations/{operation_id}
```

## 3. Execution list and detail

- Keep the current compact Execution list item.
- Add `workflow_id` as an optional list filter in REST and MCP.
- Keep Execution detail intentionally complete; it is the state, failure, recovery, deadline, and
  assignment read, not the Agent reporting result.
- Record the Jupyter-specific notebook projection fields in generic Execution detail as future
  Runtime-neutrality debt; do not redesign them in this refactor.

## 4. Step reads

- Keep the Execution-wide Step list and Operation-scoped Step list as separate useful scopes.
- Keep `result_ref` in Step detail. It already exists in the current `ExecutionStepResponse`.
- Step list summaries remain bounded and do not add `result_ref`; Agent result consumption uses the
  Result APIs.
- Replace the current aggregate load plus in-memory Step search with a dedicated
  `step(execution_id, step_id)` query.
- Do not add an MCP `execution_step_get` Tool in this refactor. This intentional REST/MCP surface
  difference must be documented; Agent MCP clients use the Result Tools.

## 5. Operation reads

Introduce `ExecutionOperationSummaryResponse` for the Operation list. Keep the existing full model
for Operation detail.

The summary contains:

- `operation_id`, `operation_number`, and `sequence_range`
- aggregate result status and safe error
- lifecycle timestamps and `step_count`
- the complete audit field set

It omits schema version, timeout, arbitrary metadata, and Attempt binding.

Correct the Operation detail description so it does not call the detail endpoint a Result read.

## 6. Compact Result contracts

Execution Result and Operation Result share the same compact Operation and Step models.

The compact Execution header contains:

- `execution_id`
- `state.status`
- `state.version`

The compact Operation contains:

- `operation_id`, `operation_number`, and `sequence_range`
- aggregate result and lifecycle
- compact Steps

Each compact Step contains:

- `step_id`, `sequence`, and tool lineage
- status, bounded output summary, canonical `result_ref`, and safe error
- lifecycle timestamps

Remove repeated parent identifiers, request-time source/hash/timeout fields, metadata, and nested
audit fields. Keep aggregate Operation errors and Step errors because they answer different
questions.

Execution Result contains current compact Operations, Attempt summaries, and Artifact summaries. It
does not embed Step Attempts or full Artifact details. Operation Result contains the compact
Execution header and one compact Operation.

Replace the current per-Operation and per-Attempt query loops with bounded bulk queries.

## 7. Attempt history

Keep Attempt list and Attempt detail unchanged. Change the paginated Attempt Step item so its
result includes the canonical `result_ref`. This preserves access to prior Attempt outputs after
Step Attempts are removed from consolidated Execution Result.

Do not add a separate Attempt Result or Step Attempt detail endpoint in this refactor.

## 8. Event history

Keep the Event list as an operational/audit view of durable Outbox events and Redis publication
state. It does not replace the Agent-owned Redis consumer.

Add optional `event_type` and `delivery_status` filters only if they can be implemented without
changing cursor ordering. Preserve ascending event chronology and opaque cursors.

## 9. Artifact list, detail, creation, and download

- Keep the existing compact Artifact list and full Artifact detail.
- Reduce the Artifact list default limit from 500 to 100; keep the accepted maximum of 1000.
- Clarify that Artifact creation materializes Agent-authored UTF-8 text. Runtime-produced binary
  Artifacts continue to be discovered and registered by the Worker.

Add a storage-neutral download endpoint:

```http
GET /api/v1/artifacts/{artifact_id}/content
```

The endpoint covers registered NOTEBOOK, REPORT, PLOT, MODEL, DATASET, LOG, METRIC, and OTHER
Artifacts stored on Runtime-owned PV storage. It must:

- resolve content exclusively from persisted Artifact storage metadata;
- reject unavailable, incomplete, deleted, or path-unsafe Artifacts;
- stream bytes without buffering the complete file in Executor memory;
- set `Content-Type`, `Content-Length`, `Content-Disposition`, `ETag`, and checksum-related headers
  from verified metadata;
- support a single HTTP `Range` request and return `206`, `Content-Range`, and `Accept-Ranges`;
- preserve Runtime Target affinity when reading Runtime-owned PV storage and fail over only to a
  target that shares the same Runtime storage;
- define explicit behavior per storage type. This refactor implements PV through Runtime storage
  access. S3 returns a stable unsupported-content error until an S3 adapter or controlled
  redirect/presigned-URL policy is implemented; stored credentials are never exposed.

Notebook files are already registered as NOTEBOOK Artifacts at finalization, so no notebook-only
download route is added. MCP does not expose raw Artifact download bytes; this REST-only exception
must be documented. MCP returns Artifact metadata and clients use an authorized REST download when
needed.

## 10. Notebook reads

Notebook reads are audit/convenience reads of the Runtime-owned notebook, not the authoritative
Agent Step-result channel.

Replace `brief` and `detailed` with:

- `SUMMARY` (default): source preview, line count, metadata, and output summary; no raw outputs.
- `FULL`: full cell source and complete notebook outputs for every cell in the requested page.

An explicit FULL request is intentionally complete, including text, display data, images, errors,
and other outputs represented by the notebook. It may be expensive. Pagination still applies.

Use separate summary and full cell response models so an omitted output is never confused with an
empty output list. Keep the single-cell endpoint as a full cell read, including complete outputs by
default.

Remove `limit=0`; enforce `1 <= limit <= 200` for REST and MCP. Preserve index-based pagination.

## 11. REST and MCP surface

Keep shared Pydantic response models. Intentional differences are:

- REST obtains parent identifiers from URL paths; MCP request wrappers include them.
- MCP notebook cell output uses protocol-native text/image Content blocks where required.
- REST alone provides Step detail in this refactor.
- REST alone streams Artifact content/download bytes.

Document these absences explicitly rather than implying complete surface parity.

## 12. Documentation correction

Remove the stale, unimplemented output-body routes from all documentation:

```text
GET /executions/{execution_id}/outputs
GET /executions/{execution_id}/outputs/{output_id}
GET /executions/{execution_id}/outputs/{output_id}/representations/{representation_id}/content
```

State explicitly that Executor provides no public Execution-output body REST API and no equivalent
MCP Tool. Step output bodies are read from checksum-verified shared-PV result manifests. Artifact
content download is a separate registered-Artifact capability and must not be described as a Step
output API.

Update REST, MCP Tool descriptions, OpenAPI summaries/error responses, Agent integration docs,
examples, and tests together.

## Implementation order

1. Add shared compact and summary contracts with mapping tests.
2. Add cardinality settings and transactional enforcement.
3. Add direct Step and bulk Result query methods.
4. Refactor Execution/Operation Result and Attempt Step history.
5. Refactor Operation and Notebook list/detail contracts.
6. Add storage-neutral Artifact content streaming and Range support.
7. Align REST and MCP Tools and intentional exceptions.
8. Update all documentation and examples.
9. Run migrations only if persistence changes prove necessary; contract-only changes should not
   create a migration.
10. Run Ruff, ty, unit/integration tests, REST OpenAPI assertions, MCP discovery/call tests, and
    local Jupyter E2E for SINGLE and MULTI.

## Change map

- `config.py`: Step cardinality settings.
- `interfaces/contracts.py`: compact Result, Operation summary, Attempt Step result-reference, and
  Notebook summary/full contracts.
- `interfaces/http/executions.py`: filters, Location, Step query, Notebook modes, Artifact content,
  limits, and route descriptions.
- `interfaces/http/schemas.py`: only transport-specific download/query helpers if needed.
- `interfaces/mcp/server.py` and `interfaces/mcp/schemas.py`: shared contract alignment and
  documented omissions.
- `application/execution_queries.py` and `infrastructure/execution_queries.py`: direct Step,
  workflow filter, bulk Result reads, and optional Event filters.
- `application/execution_results.py`: compact bundle composition without N+1 query loops.
- `application/notebook_queries.py`: SUMMARY/FULL semantics and bounded pagination.
- `domain/runtime.py`, `infrastructure/runtime_storage.py`, and Runtime drivers: ranged file-content
  streaming abstraction.
- `test_harness/jupyter/extension`: path-safe ranged file-content handler for Runtime-owned PV
  files; this avoids loading complete binary Artifacts through the Jupyter Contents JSON API.
- `tests/`: contract, query-count, range, path traversal, MIME/checksum, OpenAPI, MCP, and E2E
  coverage.
- `docs/`: REST/MCP/Agent guides and removal of stale Output APIs.

## Acceptance checks

- SINGLE terminal Result can locate every current Step source/output file without another metadata
  API call.
- MULTI Operation Result supplies the authoritative Execution version for the next command.
- Retried historical Step outputs remain reachable through Attempt Step history.
- Result responses do not repeat Step Attempts, full Artifact details, request source metadata, or
  nested audit fields.
- No Result query performs one database round trip per Operation or Attempt.
- SUMMARY Notebook reads contain no raw outputs; FULL reads contain complete source and outputs for
  the requested page.
- Notebook, Report, image, and representative binary Artifact downloads preserve bytes, MIME type,
  checksum/ETag, and Range semantics without full-response buffering.
- REST OpenAPI and MCP Tool discovery match their implemented surfaces.
- No documentation advertises an Execution output-body API.
