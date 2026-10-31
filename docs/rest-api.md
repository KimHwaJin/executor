# Executor REST API

All endpoints are under `/api/v1`. Command endpoints return `202 Accepted`; PostgreSQL remains the
authoritative state store and execution happens asynchronously through the Worker stream.

| Method | Path | Purpose |
|---|---|---|
| POST | `/executions` | Create an Execution and its initial Operation |
| GET | `/executions` | Cursor-paginated Execution history |
| GET | `/executions/{execution_id}` | Current Execution detail |
| GET | `/executions/{execution_id}/result` | Consolidated Operations, Steps, Attempts, and Artifacts |
| POST | `/executions/{execution_id}/operations` | Append one Operation to a waiting MULTI Execution |
| POST | `/executions/{execution_id}/finalize` | Finalize a waiting MULTI Execution |
| POST | `/executions/{execution_id}/cancel` | Request cancellation |
| POST | `/executions/{execution_id}/retry` | Retry a retryable failed SINGLE Execution |
| GET | `/executions/{execution_id}/steps` | Current Step summaries |
| GET | `/executions/{execution_id}/steps/{step_id}` | One current Step with canonical result reference |
| GET | `/executions/{execution_id}/operations` | Operation history |
| GET | `/executions/{execution_id}/operations/{operation_id}` | One accepted Operation detail |
| GET | `/executions/{execution_id}/operations/{operation_id}/result` | Operation and all Step results |
| GET | `/executions/{execution_id}/operations/{operation_id}/steps` | Current Step summaries for one Operation |
| GET | `/executions/{execution_id}/attempts` | Attempt history |
| GET | `/executions/{execution_id}/attempts/{attempt_id}` | One immutable Attempt detail |
| GET | `/executions/{execution_id}/attempts/{attempt_id}/steps` | Immutable Step results for one Attempt |
| GET | `/executions/{execution_id}/events` | Integration event history |
| GET | `/executions/{execution_id}/diagnostics` | Cursor-paginated failure observations; [field contract](../dev_docs/execution-diagnostics.md) |
| GET | `/executions/{execution_id}/artifacts` | Artifact history |
| POST | `/executions/{execution_id}/artifacts` | Materialize Agent-authored text on Runtime storage |
| GET | `/artifacts/{artifact_id}` | One registered Artifact and lineage |
| GET | `/artifacts/{artifact_id}/content` | Stream registered PV Artifact bytes with optional Range |
| GET | `/executions/{execution_id}/notebook` | Paginated Runtime-owned notebook summary or full cells |
| GET | `/executions/{execution_id}/notebook/cells/{cell_index}` | One complete Runtime-owned notebook cell |

## Authentication and identity

One ASGI middleware authenticates every request, REST and MCP alike. Only the probes
(`/healthz`, `/readyz`) and the API docs (`/docs`, `/redoc`, `/openapi.json`) skip it.
OpenAPI declares these headers on every route, so Swagger UI shows them as inputs and its
**Authorize** button sets `X-Api-Key`.

| Header | Rule |
|---|---|
| `X-Api-Key` | Required when `AUTH_MODE=api_key` (stg, prd). Matched by SHA-256 against `AUTH_CLIENTS`; missing or unknown is `401 UNAUTHENTICATED`. |
| `X-Tenant-Id` | Honored only for a `relay` key and only within its `allowed_tenants`, otherwise `403 FORBIDDEN`. Without it the key's `tenant` applies; a key with neither is `400 INVALID_IDENTITY`. |
| `X-User-Id` | `^[a-z0-9][a-z0-9._-]{0,63}$`, lowercased first. Required for every write. |
| `X-Project-Id`, `X-Session-Id` | `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`, never `unscoped`; a session requires a project. |

An Execution is visible only when its tenant is the caller's tenant and, when `X-User-Id` is
sent, its `user_id` is that user. Anything else is `404`, so callers cannot probe other tenants.
A read without `X-User-Id` is a system call and sees the whole tenant. Artifact routes apply the
same rule through the Artifact's Execution.

`/workerz`, `/runtime-pools`, `/runtime-targets*`, and `/maintenance*` need an `admin` key (`403` otherwise).
The same rule guards the MCP `runtime_target_*` tools.

`AUTH_MODE=header` (local and dev only; startup fails elsewhere) trusts the identity headers
without a key, defaults the tenant to `dev`, and treats every caller as admin.

```yaml
AUTH_MODE: api_key
AUTH_CLIENTS:
  - name: pr-agentic-service
    key_sha256: ["<sha256 hex of the key>"]   # two entries during rotation
    tenant: pr-agentic-service
    relay: true
    allowed_tenants: [pr-agentic-service]
  - name: ops
    key_sha256: ["<sha256 hex>"]
    admin: true
```

Write requests log `client`, `tenant_id`, and `user_id`; the key itself is never logged.

## Submit

```json
{
  "idempotency_key": "exec-1001",
  "lifecycle": {
    "operation_mode": "MULTI",
    "operation_wait_timeout_seconds": 600
  },
  "trigger": {
    "type": "INTERACTIVE",
    "actor": {"type": "AGENT", "id": "analytics-agent"}
  },
  "runtime": {"type": "JUPYTER", "profile": "basic"},
  "operation": {
    "operation_timeout_seconds": 600,
    "spec": {
      "schema_version": "1.0",
      "steps": [
        {
          "sequence": 0,
          "payload": {
            "type": "PYTHON_EXECUTE",
            "source": {"type": "INLINE", "content": "print('hello')"}
          },
          "step_timeout_seconds": 300
        }
      ]
    },
    "metadata": {}
  },
  "metadata": {}
}
```

The Execution's user, project, session, and tenant come from the identity headers above; a body
`context` or `tenant_id` is rejected with `422`. When project or session is absent, the Runtime
workspace uses the reserved path segment `unscoped`. The workspace lives under
`tenants/{tenant_id}/users/{user_id}/projects/{project}/sessions/{session}/executions/{id}`.

The tenant (`^[a-z][a-z0-9-]{1,62}[a-z0-9]$`, not `dlq` or `work`) is immutable and part of the
idempotency fingerprint. Its events go only to `{REDIS_EVENT_STREAM}.{tenant_id}`.

SINGLE rejects `operation_wait_timeout_seconds`. MULTI requires it and supports only INTERACTIVE
triggering. `operation_timeout_seconds` limits all Steps in that Operation; each
`step_timeout_seconds` limits just its Step.

## Event history and gap recovery

`GET /executions/{execution_id}/events`는 `event_sequence` 오름차순으로 공개 lifecycle
이벤트를 반환한다. Agent Subscriber가 Redis에서 순번 누락을 발견했을 때만
`after_sequence={last_event_sequence}`로 누락 구간을 조회한다. 응답이 여러 페이지면
`next_cursor`를 그대로 다음 요청의 `cursor`로 전달한다. 정상적인 연속 Redis 전달에는
이 API를 호출할 필요가 없다. 이벤트 Envelope와 payload는 영구 `execution_events`에서
반환되며, 단기 Outbox 전송 행이 정리된 오래된 이벤트는 `delivery=null`이다.

전체 이벤트 Envelope, 타입별 payload 및 소비 알고리즘은 다음 문서를 참고한다.

- [Redis Execution Event Contract 1.0](../dev_docs/redis-execution-events.md)
- [Agent Execution Event Consumer Guide](../dev_docs/agent-execution-event-consumer-guide.md)

The command receipt includes Executor-owned IDs:

```json
{
  "execution_id": "...",
  "operation": {
    "operation_id": "...",
    "steps": [{"sequence": 0, "step_id": "..."}]
  },
  "state": {"status": "QUEUED", "version": 0},
  "created_by_type": "AGENT",
  "created_by": "analytics-agent",
  "updated_by_type": "AGENT",
  "updated_by": "analytics-agent",
  "created_at": "...",
  "updated_at": "..."
}
```

## Append and finalize MULTI work

Append uses `POST /executions/{execution_id}/operations` with `idempotency_key`, the last observed
`expected_version`, optional `operation_timeout_seconds`, `spec`, optional `metadata`, and `actor`.
The spec may contain one or more contiguous Steps. Executor runs every accepted Step and then
transitions to `WAITING_FOR_OPERATION`, where the Agent can inspect results and submit another
Operation.

Finalize uses `POST /executions/{execution_id}/finalize` with `idempotency_key`,
`expected_version`, and `actor`. Acceptance transitions to `FINALIZING`; the Worker asks the
selected Runtime Driver to persist its final execution record, releases the retained Runtime
session, and reaches a terminal state. The Jupyter driver writes the final notebook.

All list endpoints use opaque cursor pagination. Clients must return `next_cursor` unchanged.
OpenAPI is available at `/openapi.json`, Swagger UI at `/docs`, and ReDoc at `/redoc`.

Execution history is always limited to the caller's tenant and, with `X-User-Id`, that user. It
can be further filtered by `project_id`, `session_id`, `trigger_type`, and `status`. Detail and
history items expose `tenant_id`, the Execution `context`, and `trigger{type,
actor}` (the immutable submit actor) and the submit `metadata` object, which `execution.completed`
also carries. Submit `metadata` holds at most 32 keys matching `^[a-z][a-z0-9_]{0,63}$` and
4096 serialized bytes. Executor accepts at most
`EXECUTION_MAX_STEPS_PER_OPERATION` Steps in one Operation and
`EXECUTION_MAX_STEPS_PER_EXECUTION` Steps across one Execution. The configured defaults are 100
and 1000. Going over either answers `422 INVALID_EXECUTION_SPEC`.

## Errors

Every error body is `{"error": {"code", "message", "details?"}}`. `details` appears when a
request value is at fault: each entry has `location` (`body` or `header`, then the field path),
`message`, a stable snake_case `type`, and `context` with the numbers behind it (for example
`limit` and `actual`). Request-shape failures (`REQUEST_VALIDATION_ERROR`) and rule failures
(`INVALID_EXECUTION_SPEC`) both use 422 and this shape; 409 is kept for requests that are valid
but conflict with the current state or version. MCP Tool errors read `[CODE] message
details=[...]` with the same JSON. The full list of types is in
[dev_docs/errors.md](../dev_docs/errors.md).

Execution detail exposes notebook projection separately under `workspace.notebook_projection`.
The state begins as `NOT_STARTED`, becomes `PENDING` only while a projection is attempted, and
finishes as `SUCCEEDED` or `FAILED`. Code execution and its shared-volume result remain successful
even when this user-facing notebook projection fails.

## Result retrieval and report materialization

Redis events are wake-up notifications. Step events contain `output_summary`,
`result_available=true`, and a `result_ref`, but never full text or image payloads. After an
Operation or terminal event, the Agent calls the matching compact result endpoint. The Execution
Result contains only the Execution state/version, compact Operations and current Steps, Attempt
summaries, and Artifact summaries. The Operation Result uses the same compact Operation and Step
models. Step results return the bounded summary plus a `SHARED_PV` reference. The Agent safely
resolves the relative manifest path under its configured shared root and verifies every checksum
before using text, structured data, image, or binary files. There is no public Step output-body
REST API and no equivalent MCP Tool; Redis, PostgreSQL, and LLM Tool results remain bounded.
Direct current-Step detail is also REST-only in this contract; MCP clients use the compact Result
Tools. This is an intentional surface difference rather than an omitted Tool.

`POST /executions/{execution_id}/artifacts` accepts idempotent Agent-authored UTF-8 content from an
INLINE source or input-PV PATH. `DATASET` and `MODEL` are intentionally excluded from this
text-materialization command; Runtime code or the Artifact Manifest registers those outputs. A
REPORT defaults to `reports/final-report.md`; callers do not
choose an arbitrary Runtime target path. `append_to_notebook=true` also appends the Markdown as a
notebook cell. Materialization is allowed only after a successful Execution.

`GET /artifacts/{artifact_id}/content` is separate from Step-result retrieval. It streams a
registered PV Artifact without buffering the complete body and supports one `Range: bytes=...`
request. S3 content has a stable unsupported response until an S3 adapter or redirect policy is
configured. Raw Artifact byte download is intentionally REST-only; MCP returns metadata.

Omitting `Range` returns the **current complete file** (`200`), not the byte count captured when
the Artifact was registered. A valid single range returns `206`; an unsatisfiable/unsupported
range returns `416` with `Content-Range: bytes */<current-size>`. Empty files return an empty `200`
without Range. Length, range, SHA-256 and ETag come from one opened Runtime file, not DB metadata.
Artifact list/detail metadata remains the registration observation; downloading does not mutate
historical evidence, audit fields, or Execution events. No DB migration is required.

The managed `notebooks/execution.ipynb` is registered as an Execution-scoped
`NOTEBOOK`/`AVAILABLE` Artifact as soon as preparation first saves it. It is
downloadable during running, MULTI waiting, failure and cancellation as well as
success. Availability does not assert execution success or complete cell output.
Repeated Executor saves refresh the observed metadata under the same Artifact ID
without creating Step/Attempt snapshots. These producer references are null for
new notebook entries. Manual edits are read at download time without changing
Step-result evidence. Previously unregistered historical files are not backfilled.

The Runtime hashes that descriptor with bounded reads before sending headers, then streams the
requested bytes from the same descriptor. This preserves a full-file SHA-256/strong ETag but adds
one full-file read before **each** request (including small Range requests); no entire-file RAM
buffer or download copy is created. Atomic notebook replacement preserves an existing reader on
POSIX. Arbitrary in-place writes by users/tools are not snapshot-isolated: detected changes fail
setup or abort an incomplete stream. Finish saving before downloading such files. Multiple Range
requests open independently; compare ETags and discard/restart parts if they differ.

For BFF proxy requirements, error handling and examples, see
[`dev_docs/get-artifact-content.md`](../dev_docs/get-artifact-content.md).

Notebook reads are audit and convenience APIs, not the authoritative Agent result channel.
`view=SUMMARY` is the default and returns source previews and output summaries without raw output
bodies. `view=FULL` returns complete source and every notebook output for each cell in the requested
page. The single-cell endpoint is always full. Notebook pagination requires `1 <= limit <= 200`.
