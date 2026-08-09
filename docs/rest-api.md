# Executor REST API v1

The REST facade exposes the same PostgreSQL-backed execution lifecycle as the MCP tools. REST does
not call MCP internally; both adapters invoke the same application services, ExecutionSpec
resolver, Transactional Outbox, and Worker. An Execution submitted through REST can be queried
through MCP and vice versa.

Interactive documentation is available at `/docs`, ReDoc at `/redoc`, and the OpenAPI document at
`/openapi.json`.

## Endpoints

| Method | Path | Purpose | Success |
| --- | --- | --- | --- |
| GET | `/api/v1/capabilities` | Protocol and runtime capabilities | 200 |
| POST | `/api/v1/executions` | Submit asynchronous STATIC or DYNAMIC work | 202 |
| GET | `/api/v1/executions` | List history with user/project/session/Task/status filters | 200 |
| GET | `/api/v1/executions/{execution_id}` | Get the PostgreSQL current state | 200 |
| POST | `/api/v1/executions/{execution_id}/cancel` | Request cancellation | 202 |
| POST | `/api/v1/executions/{execution_id}/retry` | Retry an eligible FAILED execution | 202 |
| POST | `/api/v1/executions/{execution_id}/continue` | Append one next DYNAMIC Step | 202 |
| POST | `/api/v1/executions/{execution_id}/finish` | Finalize a waiting DYNAMIC execution | 202 |
| GET | `/api/v1/executions/{execution_id}/steps` | List current Steps | 200 |
| GET | `/api/v1/executions/{execution_id}/steps/{step_id}` | Get one current Step | 200 |
| GET | `/api/v1/executions/{execution_id}/attempts` | List immutable Attempts and Step Attempts | 200 |
| GET | `/api/v1/executions/{execution_id}/events` | List Outbox/Redis publication history | 200 |
| GET | `/api/v1/executions/{execution_id}/artifacts` | List produced Artifacts | 200 |
| GET | `/api/v1/executions/{execution_id}/trace` | Get combined state, Attempts, events, Artifacts | 200 |
| GET | `/api/v1/artifacts/{artifact_id}` | Get one Artifact and lineage references | 200 |

List endpoints accept `limit`. Execution history additionally accepts `requested_by_user_id`,
`project_id`, `session_id`, `task_id`, and `status`. Results are newest-first and currently use a
bounded limit rather than cursor pagination.

## Submit and poll

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/executions \
  -H 'Content-Type: application/json' \
  -d '{
    "idempotency_key": "rest-submit-001",
    "mode": "STATIC",
    "trigger_type": "INTERACTIVE",
    "kernel_name": "python3",
    "source": {
      "type": "INLINE",
      "spec": {
        "schema_version": "1.0",
        "execution_plan_id": "plan-001",
        "steps": [{
          "sequence": 0,
          "plan_step_id": "plan-step-001",
          "skill_name": "data_load",
          "tool_name": "load_data",
          "input_parameters": {"product": "A"},
          "code": "print(\"hello\")"
        }]
      }
    },
    "context": {
      "requested_by_user_id": "user-001",
      "project_id": "project-001",
      "session_id": "session-001",
      "task_id": "task-001"
    },
    "metadata": {}
  }'
```

The response is `202 Accepted`, includes the new `execution_id`, and sets `Location` to its GET
resource. Tool completion is not execution completion.

```bash
curl http://127.0.0.1:8000/api/v1/executions/EXECUTION_ID
curl 'http://127.0.0.1:8000/api/v1/executions?task_id=task-001&limit=20'
curl http://127.0.0.1:8000/api/v1/executions/EXECUTION_ID/trace
```

PATH submit uses the same request except for `source`:

```json
{
  "type": "PATH",
  "path": "users/user-001/projects/project-001/sessions/session-001/plans/plan-001/source.json",
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

The path is relative to `WORKSPACE_HOST_ROOT`. The file must contain the same ExecutionSpec object
used as INLINE `source.spec` and must be atomically published and immutable after submission.

## Cancel and retry

```bash
curl -X POST http://127.0.0.1:8000/api/v1/executions/EXECUTION_ID/cancel \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"rest-cancel-001","reason":"user requested"}'

curl -X POST http://127.0.0.1:8000/api/v1/executions/EXECUTION_ID/retry \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"rest-retry-001"}'
```

Cancel returns after `CANCEL_REQUESTED` is committed. Retry is accepted only when the FAILED
Execution advertises a supported retry strategy.

## Dynamic continue and finish

After a DYNAMIC Step reaches `WAITING_FOR_NEXT_STEP`, use the returned current `version` and append
exactly one consecutive Step:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/executions/EXECUTION_ID/continue \
  -H 'Content-Type: application/json' \
  -d '{
    "idempotency_key": "rest-continue-001",
    "expected_version": 1,
    "source": {
      "type": "INLINE",
      "spec": {
        "schema_version": "1.0",
        "execution_plan_id": "plan-002",
        "steps": [{
          "sequence": 1,
          "plan_step_id": "plan-step-002",
          "code": "print(\"next cell\")"
        }]
      }
    }
  }'
```

When no more cells are needed:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/executions/EXECUTION_ID/finish \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"rest-finish-001","expected_version":3}'
```

Always read the latest Execution before continue or finish. A stale version returns `409`.

## Errors

Expected domain failures use a stable envelope:

```json
{
  "error": {
    "code": "IdempotencyConflictError",
    "message": "The submit idempotency_key was already used with a different request."
  }
}
```

- `404`: Execution, Step, or Artifact does not exist
- `409`: invalid state transition, stale version, idempotency conflict, persistence conflict
- `422`: request validation or invalid ExecutionSpec/PATH source

There is no authentication layer in Executor in the current project scope. Deploy the REST API
behind the same internal gateway/network policy as `/mcp`; do not expose it directly to an
untrusted network.
