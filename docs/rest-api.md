# Executor REST API

All endpoints are under `/api/v1`. Command endpoints return `202 Accepted`; PostgreSQL remains the
authoritative state store and execution happens asynchronously through the Worker stream.

| Method | Path | Purpose |
|---|---|---|
| POST | `/executions` | Create an Execution and its initial Operation |
| GET | `/executions` | Cursor-paginated Execution history |
| GET | `/executions/{execution_id}` | Current Execution detail |
| POST | `/executions/{execution_id}/operations` | Append one Operation to a waiting MULTI Execution |
| POST | `/executions/{execution_id}/finalize` | Finalize a waiting MULTI Execution |
| POST | `/executions/{execution_id}/cancel` | Request cancellation |
| POST | `/executions/{execution_id}/retry` | Retry a retryable failed SINGLE Execution |
| GET | `/executions/{execution_id}/steps` | Step history |
| GET | `/executions/{execution_id}/operations` | Operation history |
| GET | `/executions/{execution_id}/attempts` | Attempt history |
| GET | `/executions/{execution_id}/events` | Integration event history |
| GET | `/executions/{execution_id}/artifacts` | Artifact history |
| GET | `/executions/{execution_id}/notebook` | Runtime-owned notebook |

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
    "source": {
      "type": "INLINE",
      "spec": {
        "schema_version": "1.0",
        "steps": [
          {
            "sequence": 0,
            "payload": {"type": "CODE", "content": "print('hello')"},
            "step_timeout_seconds": 300
          }
        ]
      }
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
`expected_version`, optional `operation_timeout_seconds`, `source`, optional `metadata`, and `actor`.
The source may contain one or more contiguous Steps. Executor runs every accepted Step and then
transitions to `WAITING_FOR_OPERATION`, where the Agent can inspect results and submit another
Operation.

Finalize uses `POST /executions/{execution_id}/finalize` with `idempotency_key`,
`expected_version`, and `actor`. Acceptance transitions to `FINALIZING`; the Worker asks the
selected Runtime Driver to persist its final execution record, releases the retained Runtime
session, and reaches a terminal state. The Jupyter driver writes the final notebook.

All list endpoints use opaque cursor pagination. Clients must return `next_cursor` unchanged.
OpenAPI is available at `/openapi.json`, Swagger UI at `/docs`, and ReDoc at `/redoc`.
