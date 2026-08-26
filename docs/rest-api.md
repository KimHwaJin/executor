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
| GET | `/executions/{execution_id}/artifacts` | Artifact history |
| POST | `/executions/{execution_id}/artifacts` | Materialize Agent-authored text on Runtime storage |
| GET | `/artifacts/{artifact_id}` | One registered Artifact and lineage |
| GET | `/artifacts/{artifact_id}/content` | Stream registered PV Artifact bytes with optional Range |
| GET | `/executions/{execution_id}/notebook` | Paginated Runtime-owned notebook summary or full cells |
| GET | `/executions/{execution_id}/notebook/cells/{cell_index}` | One complete Runtime-owned notebook cell |

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
  "context": {
    "task_id": "task-100",
    "user_id": "user-100",
    "project_id": "project-100",
    "session_id": "session-100"
  },
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

`task_id` and `user_id` are required. `project_id`, `session_id`, and `workflow_id` are optional;
`session_id` requires `project_id`. When project or session is absent, the Runtime workspace uses the
reserved path segment `unscoped`. Clients cannot submit `unscoped` as an external ID.

SINGLE rejects `operation_wait_timeout_seconds`. MULTI requires it and supports only INTERACTIVE
triggering. `operation_timeout_seconds` limits all Steps in that Operation; each
`step_timeout_seconds` limits just its Step.

## Event history and gap recovery

`GET /executions/{execution_id}/events`는 `event_sequence` 오름차순으로 공개 lifecycle
이벤트를 반환한다. Agent Subscriber가 Redis에서 순번 누락을 발견했을 때만
`after_sequence={last_event_sequence}`로 누락 구간을 조회한다. 응답이 여러 페이지면
`next_cursor`를 그대로 다음 요청의 `cursor`로 전달한다. 정상적인 연속 Redis 전달에는
이 API를 호출할 필요가 없다.

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

Execution history can be filtered by `user_id`, `project_id`, `session_id`, `task_id`,
`workflow_id`, and `status`. Executor accepts at most
`EXECUTION_MAX_STEPS_PER_OPERATION` Steps in one Operation and
`EXECUTION_MAX_STEPS_PER_EXECUTION` Steps across one Execution. The configured defaults are 100
and 1000.

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
INLINE source or input-PV PATH. A REPORT defaults to `reports/final-report.md`; callers do not
choose an arbitrary Runtime target path. `append_to_notebook=true` also appends the Markdown as a
notebook cell. Materialization is allowed only after a successful Execution.

`GET /artifacts/{artifact_id}/content` is separate from Step-result retrieval. It streams a
registered PV Artifact without buffering the complete body and supports one `Range: bytes=...`
request. S3 content has a stable unsupported response until an S3 adapter or redirect policy is
configured. Raw Artifact byte download is intentionally REST-only; MCP returns metadata.

Notebook reads are audit and convenience APIs, not the authoritative Agent result channel.
`view=SUMMARY` is the default and returns source previews and output summaries without raw output
bodies. `view=FULL` returns complete source and every notebook output for each cell in the requested
page. The single-cell endpoint is always full. Notebook pagination requires `1 <= limit <= 200`.
