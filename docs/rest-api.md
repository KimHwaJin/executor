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
| POST | `/api/v1/runtime-targets` | Register/update and immediately probe a target | 200 |
| GET | `/api/v1/runtime-targets` | List targets with pool/status/enabled filters | 200 |
| GET | `/api/v1/runtime-targets/{target_id}` | Get target health and capacity | 200 |
| GET | `/api/v1/runtime-pools` | Summarize health and capacity by pool | 200 |
| POST | `/api/v1/runtime-targets/{target_id}/probe` | Run an immediate health probe | 200 |
| POST | `/api/v1/runtime-targets/{target_id}/drain` | Stop new assignment, preserve running work | 200 |
| POST | `/api/v1/runtime-targets/{target_id}/activate` | Enable and probe before scheduling | 200 |
| DELETE | `/api/v1/runtime-targets/{target_id}` | Durable soft delete (`OFFLINE`, disabled) | 200 |
| POST | `/api/v1/runtime-targets/{target_id}/purge` | Restricted permanent removal | 200 |

List endpoints accept `limit` and an opaque `cursor`. They return `items`, `next_cursor`, and
`has_more`; pass `next_cursor` unchanged to retrieve the next keyset page. Execution history
additionally accepts `user_id`, `project_id`, `session_id`, `task_id`, and `status`.
Keep the same filters for every page. Execution history is newest-first; Steps, Attempts, events,
and Artifacts preserve their natural chronological/sequence order.

All mutation bodies require `actor: {type, id}`. Supported actor types are `USER` and `BATCH`.
`context.user_id` is the owner of the Execution and its results. Interactive submit requires a
`USER` actor with `actor.id == context.user_id`. Batch submit requires a `BATCH` actor whose ID
identifies the schedule or manual batch trigger and may differ from `context.user_id`. Batch submit
also requires `context.workflow_id`.
Interactive submissions require `USER`, while batch submissions require `BATCH`. Responses expose
`created_at`, `updated_at`, `created_by_type`, `created_by`, `updated_by_type`, and `updated_by` on
audited resources. Execution detail, list, and trace responses expose the immutable `runtime_type`
and `runtime_profile`. Attempt history snapshots both fields so it remains self-contained even if
fleet configuration changes later.

## Submit and poll

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/executions \
  -H 'Content-Type: application/json' \
  -d '{
    "idempotency_key": "rest-submit-001",
    "mode": "STATIC",
    "trigger_type": "INTERACTIVE",
    "actor": {"type": "USER", "id": "user-001"},
    "runtime_type": "JUPYTER",
    "runtime_profile": "basic",
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
      "user_id": "user-001",
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
curl 'http://127.0.0.1:8000/api/v1/executions?task_id=task-001&limit=20&cursor=NEXT_CURSOR'
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
  -d '{
    "idempotency_key":"rest-cancel-001",
    "reason":"user requested",
    "actor":{"type":"USER","id":"user-001"}
  }'

curl -X POST http://127.0.0.1:8000/api/v1/executions/EXECUTION_ID/retry \
  -H 'Content-Type: application/json' \
  -d '{
    "idempotency_key":"rest-retry-001",
    "actor":{"type":"USER","id":"user-001"}
  }'
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
    "actor": {"type": "USER", "id": "user-001"},
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
  -d '{
    "idempotency_key":"rest-finish-001",
    "expected_version":3,
    "actor":{"type":"USER","id":"user-001"}
  }'
```

Always read the latest Execution before continue or finish. A stale version returns `409`.

## Runtime fleet administration

Registering a target stores its credential encrypted and probes it immediately. The credential is
required for a new target and optional when updating an existing name. Neither credential nor
connection configuration is included in responses. `runtime_type` cannot be changed for an
existing target name; create a new target for a different Runtime Driver.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/runtime-targets \
  -H 'Content-Type: application/json' \
  -d '{
    "idempotency_key":"target-register-001",
    "name":"interactive-jupyter-02",
    "runtime_type":"JUPYTER",
    "connection_config":{"endpoint":"http://jupyter-02:8888"},
    "credential":"replace-with-real-token",
    "pool":"INTERACTIVE",
    "max_concurrent_executions":2,
    "actor":{"type":"USER","id":"operator-001"}
  }'

curl 'http://127.0.0.1:8000/api/v1/runtime-targets?pool=INTERACTIVE&status=ACTIVE&enabled=true&limit=50'
curl http://127.0.0.1:8000/api/v1/runtime-pools
```

`probe` only refreshes health. `drain` keeps the target enabled but excludes it from new
assignments while current work finishes. `activate` enables and probes it, becoming `ACTIVE` only
when healthy. `DELETE` is a soft delete: the row remains queryable as disabled and `OFFLINE` so all
Execution and Attempt references remain intact.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/runtime-targets/TARGET_ID/drain \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"server-drain-001","actor":{"type":"USER","id":"operator-001"}}'

curl -X DELETE http://127.0.0.1:8000/api/v1/runtime-targets/TARGET_ID \
  -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"server-remove-001","actor":{"type":"USER","id":"operator-001"}}'
```

Hard purge is intentionally narrow. The target must already be disabled and `OFFLINE`, the
request must repeat its exact registered name as `confirmation_name`, and neither Execution nor
Attempt history may reference it. The environment-configured default target cannot be purged.
Successful purge removes the credential-bearing registry row but retains an immutable audit
tombstone; it never cascades into execution history.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/runtime-targets/TARGET_ID/purge \
  -H 'Content-Type: application/json' \
  -d '{
    "idempotency_key":"server-purge-001",
    "confirmation_name":"interactive-jupyter-02",
    "actor":{"type":"USER","id":"operator-001"}
  }'
```

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

- `404`: Execution, Step, Artifact, or Runtime Target does not exist
- `409`: invalid state transition, stale version, idempotency/persistence conflict, unsafe purge
- `422`: request validation, invalid cursor, or invalid ExecutionSpec/PATH source

There is no authentication layer in Executor in the current project scope. Deploy the REST API
behind the same internal gateway/network policy as `/mcp`; do not expose it directly to an
untrusted network.
