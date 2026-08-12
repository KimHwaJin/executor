# API Contract Review

This document records API contract decisions one item at a time. A section marked `UNDER REVIEW`
is a draft and must not be treated as an implementation contract. A section becomes implementable
only after it is marked `ACCEPTED`.

## Shared rules

- Persistent object responses keep audit fields at the top level using the common names:
  `created_by_type`, `created_by`, `updated_by_type`, `updated_by`, `created_at`, and `updated_at`.
- REST responses and MCP Tool structured-content payloads use the same shared Pydantic contracts.
  The official MCP SDK owns protocol envelopes, JSON-RPC, Tool output schemas, and Tool discovery.
- Credentials and other secret values are never returned.
- Existing fields are not retained as compatibility aliases when a replacement contract is
  accepted.
- When an API or MCP Tool is accepted for removal, remove its entire exclusive feature slice:
  route/Tool registration, transport schemas, application/query contracts, infrastructure query
  composition, capabilities entries, tests, examples, README and API documentation references.
  Shared primitives used by remaining endpoints must be retained.

## Review queue

1. Runtime Target response — `ACCEPTED`
2. Runtime Pool response — `ACCEPTED`
3. Execution response and summary — `ACCEPTED`
4. Execution Step response — `ACCEPTED`
5. Execution Attempt response — `ACCEPTED`
6. Execution Step Attempt response — `ACCEPTED`
7. Execution Event response — `ACCEPTED`
8. Execution Artifact response — `ACCEPTED`
9. Execution Trace response semantics — `ACCEPTED: REMOVE`
10. Capabilities response — `ACCEPTED: REMOVE`
11. Pagination conventions — `ACCEPTED`
12. Request contracts and structural issues — `ACCEPTED`

## 1. Runtime Target response

Status: `ACCEPTED`

Scope:

- REST Runtime Target create/update, list, detail, probe, drain, activate, and soft-delete responses.
- MCP Runtime Target create/update, list, detail, probe, remove, and state-change responses.
- Database schema and internal scheduling models are outside this response-only change.

Draft shape:

```json
{
  "target_id": "6976e9c1-b661-451d-a15a-aaf910beafbd",
  "name": "local-jupyter",
  "runtime": {
    "type": "JUPYTER",
    "pool": "INTERACTIVE",
    "connection_config": {
      "endpoint": "http://jupyter:8888"
    },
    "supported_profiles": ["basic", "ml"]
  },
  "state": {
    "status": "ACTIVE",
    "enabled": true,
    "accepting_new_executions": true,
    "drain_complete": false
  },
  "capacity": {
    "max_concurrent_executions": 2,
    "active_execution_count": 0,
    "available_capacity": 2,
    "active_session_count": 0
  },
  "health": {
    "last_check_at": "2026-08-12T07:37:38Z",
    "last_error": null
  },
  "resources": {
    "observed_at": "2026-08-12T07:37:38Z",
    "last_check_at": "2026-08-12T07:37:38Z",
    "last_error": null,
    "fresh": true,
    "source": "CGROUP_V2",
    "estimated": false,
    "process_count": 2,
    "pressure_score": 0.026518,
    "cpu": {
      "used_cores": 0.007124,
      "capacity_cores": 2.0,
      "utilization": 0.003562
    },
    "memory": {
      "used_bytes": 113893376,
      "capacity_bytes": 4294967296,
      "utilization": 0.026518
    },
    "errors": []
  },
  "created_by_type": null,
  "created_by": null,
  "updated_by_type": null,
  "updated_by": null,
  "created_at": "2026-08-12T04:56:17Z",
  "updated_at": "2026-08-12T07:37:38Z"
}
```

Rationale:

- `runtime` describes what execution environment the target provides.
- `state` describes registry and scheduling state.
- `capacity` contains configured capacity and current reservation/session counts.
- `health` describes core connectivity and kernel-profile probing.
- `resources` describes the independently fallible CPU and memory observation.
- Audit fields remain top-level according to the shared API rule.

Accepted decisions:

1. Return non-secret `connection_config` in `runtime`; credentials and tokens remain excluded.
2. Use concise nested names `type` and `pool` because the containing object already supplies the
   `runtime` context.
3. Keep `active_session_count` in `capacity` as an operationally useful observed count, while
   documenting that Executor admission uses its own reservation count.
4. Always return the `resources` object. Before the first successful observation, or after a
   resource probe failure with no retained observation, keep measurement values null and expose
   `fresh=false`, `last_check_at`, and `last_error`. This avoids a second structural branch for
   clients.

Nullable resource example:

```json
{
  "resources": {
    "observed_at": null,
    "last_check_at": "2026-08-12T07:37:38Z",
    "last_error": "Resource probe failed (RuntimeDriverError)",
    "fresh": false,
    "source": null,
    "estimated": null,
    "process_count": null,
    "pressure_score": null,
    "cpu": {
      "used_cores": null,
      "capacity_cores": null,
      "utilization": null
    },
    "memory": {
      "used_bytes": null,
      "capacity_bytes": null,
      "utilization": null
    },
    "errors": []
  }
}
```

## 2. Runtime Pool response

Status: `ACCEPTED`

Scope:

- REST `GET /api/v1/runtime-pools` response items.
- A future MCP Runtime Pool query should reuse the same domain shape if added.
- Runtime Pool is a computed aggregate, so it has no audit fields.

Draft shape:

```json
{
  "runtime": {
    "type": "JUPYTER",
    "pool": "INTERACTIVE"
  },
  "targets": {
    "total": 3,
    "enabled": 3,
    "active": 2,
    "draining": 1,
    "offline": 0
  },
  "capacity": {
    "configured": 6,
    "schedulable": 4,
    "reserved_execution_count": 2,
    "available": 2
  },
  "state": {
    "accepting_new_executions": true,
    "saturated": false
  }
}
```

Rationale:

- `runtime` identifies the driver type and strict scheduling partition.
- `targets` summarizes registry state counts.
- `capacity` summarizes configured, schedulable, reserved, and available slots.
- `state` exposes immediately actionable aggregate booleans.

Accepted decisions:

1. Rename `active_execution_count` to `reserved_execution_count`, because it includes running,
   waiting, and retained-retry reservations rather than only actively executing work.
2. Define `saturated=true` only when an ACTIVE target exists but all schedulable slots are used;
   an empty or fully OFFLINE pool would remain `saturated=false` and
   `accepting_new_executions=false`.
3. Omit aggregate `health` from Runtime Pool. Target-level health timestamps and errors remain
   available through Runtime Target list/detail responses. A single aggregate timestamp is
   ambiguous and does not improve pool admission decisions; the target status counts already
   expose whether the pool includes ACTIVE, DRAINING, or OFFLINE targets.

## 3. Execution response and summary

Status: `ACCEPTED`

Scope:

- REST execution submit, get, cancel, retry, continue, and finish responses.
- REST execution list item responses.
- Corresponding MCP Execution tools and list items.
- Request contracts are not included in this section.

Draft detail shape:

```json
{
  "execution_id": "460fbba5-4ad5-46ba-82b0-63899a074816",
  "mode": "STATIC",
  "trigger_type": "INTERACTIVE",
  "context": {
    "user_id": "user-1",
    "project_id": "project-1",
    "session_id": "session-1",
    "task_id": "task-1",
    "execution_plan_id": "plan-1",
    "workflow_id": null
  },
  "source": {
    "type": "INLINE",
    "path": null,
    "sha256": "..."
  },
  "runtime": {
    "type": "JUPYTER",
    "pool": "INTERACTIVE",
    "profile": "basic",
    "target_id": "6976e9c1-b661-451d-a15a-aaf910beafbd",
    "session_id": "kernel-id"
  },
  "state": {
    "status": "RUNNING",
    "version": 3,
    "cancellation_reason": null
  },
  "workspace": {
    "path": "users/user-1/projects/project-1/sessions/session-1/executions/...",
    "notebook_path": ".../notebooks/execution.ipynb"
  },
  "failure": null,
  "retry": {
    "strategy": "FROM_FAILED_STEP",
    "count": 0,
    "from_sequence": null,
    "retained_runtime_session_until": null
  },
  "recovery": {
    "count": 0,
    "runtime_session_cleanup_status": "NOT_REQUIRED"
  },
  "deadlines": {
    "dynamic_wait_expires_at": null,
    "execution_expires_at": "2026-08-17T07:37:38Z"
  },
  "lifecycle": {
    "started_at": "2026-08-12T07:37:38Z",
    "finished_at": null
  },
  "steps": [],
  "created_by_type": "USER",
  "created_by": "user-1",
  "updated_by_type": "USER",
  "updated_by": "user-1",
  "created_at": "2026-08-12T07:37:37Z",
  "updated_at": "2026-08-12T07:37:38Z"
}
```

Failure shape when present:

```json
{
  "failure": {
    "type": "TOOL_ERROR",
    "message": "ValueError: invalid input"
  }
}
```

Draft list-summary rule:

- Reuse `context`, `runtime`, `state`, `failure`, `retry`, and `lifecycle` shapes.
- Omit `source`, `workspace`, `recovery`, `deadlines`, and `steps` from list items.
- Add top-level `step_count` to list items.
- Keep the common top-level audit fields.

Accepted decisions:

1. Keep `mode` and `trigger_type` top-level because they define the Execution request itself.
2. Return `workspace` as an always-present object with nullable `path` and `notebook_path` before
   assignment.
3. Keep `retry` and `recovery` separate: retry is a user/domain execution policy, while recovery
   describes Executor infrastructure recovery and Runtime session cleanup.
4. Keep timestamps specific to execution progress under `lifecycle` and `deadlines`, while common
   persistence timestamps remain top-level.
5. Use the proposed reduced list summary rather than returning the full detail object for every
   item.

Runtime identifier semantics:

- `runtime.target_id` is the Executor registry UUID of the concrete Runtime Target selected for
  the Execution. For Jupyter, it identifies one registered Jupyter server, not a pool or profile.
  It is null while the Execution is queued and no target has been reserved.
- `runtime.session_id` is the runtime-native session identifier created inside the selected
  target. For Jupyter, it is the Jupyter kernel ID. It is null before kernel creation and can
  become null again after successful cleanup. A DYNAMIC execution waiting for the next Step, or a
  failed execution retaining a kernel for Step retry, keeps it populated.

Execution version semantics:

- `state.version` is an Execution aggregate state revision, not a revision of every database
  column and not an ExecutionPlan version.
- It starts at `0` and increases on meaningful workflow transitions such as claim/start, dynamic
  pause, cancel request, retry request, dynamic continue/finish acceptance, terminal finalization,
  and recovery/failure transitions.
- Heartbeats, Step output persistence, and the internal recording of a newly created Runtime
  session do not independently increase it.
- DYNAMIC continue and finish requests must send the last observed version as `expected_version`.
  A mismatch rejects a stale Agent decision instead of appending or finishing against an outdated
  Execution state.

## 4. Execution Step response

Status: `ACCEPTED`

Scope:

- Steps embedded in Execution detail responses.
- REST Step list/detail responses.
- MCP Step list responses.

Draft shape:

```json
{
  "step_id": "20f8a8d3-1f57-4ff8-9a9d-5bf58e7f30fb",
  "sequence": 0,
  "code_hash": "...",
  "plan": {
    "execution_plan_id": "plan-1",
    "plan_step_id": "plan-step-1"
  },
  "tool": {
    "skill_name": "data_load",
    "tool_name": "load_data",
    "input_parameters": {}
  },
  "result": {
    "status": "SUCCEEDED",
    "outputs": [],
    "error_message": null
  },
  "lifecycle": {
    "started_at": "2026-08-12T07:37:38Z",
    "finished_at": "2026-08-12T07:37:42Z"
  },
  "created_by_type": "USER",
  "created_by": "user-1",
  "updated_by_type": "USER",
  "updated_by": "user-1",
  "created_at": "2026-08-12T07:37:37Z",
  "updated_at": "2026-08-12T07:37:42Z"
}
```

Accepted decisions:

1. Keep `code_hash` top-level as Step identity/integrity metadata; never return executable code in
   normal Step responses.
2. Include `input_parameters` in `tool`; it is currently persisted but missing from the current
   Execution Step response.
3. Use `result.error_message`, or use the same nullable `failure: {type, message}` pattern as an
   Execution. A Step currently stores no structured failure type, so a Step `failure` could only
   contain a message unless persistence is expanded.
4. Standardize the identifier as `step_id` in both REST and MCP; MCP currently exposes `id`.

## 5. Execution Attempt response

Status: `ACCEPTED`

Scope:

- REST Execution Attempt list items embedded with their Step Attempts.
- MCP Execution Attempt list items embedded with their Step Attempts.
- Step Attempt response details are reviewed separately in section 6.

Draft shape:

```json
{
  "attempt_id": "b24447fc-a3d7-4d20-ad7d-2882f288ca09",
  "execution_id": "460fbba5-4ad5-46ba-82b0-63899a074816",
  "attempt_number": 2,
  "runtime": {
    "type": "JUPYTER",
    "profile": "basic",
    "target_id": "6976e9c1-b661-451d-a15a-aaf910beafbd",
    "session_id": "kernel-id"
  },
  "state": {
    "status": "RUNNING"
  },
  "lease": {
    "owner": "executor-worker-1",
    "expires_at": "2026-08-12T07:38:38Z",
    "heartbeat_at": "2026-08-12T07:37:53Z"
  },
  "failure": null,
  "recovery": {
    "retry_strategy": "FROM_FAILED_STEP",
    "runtime_session_cleanup_status": "NOT_REQUIRED"
  },
  "lifecycle": {
    "started_at": "2026-08-12T07:37:38Z",
    "finished_at": null
  },
  "steps": [],
  "created_by_type": "USER",
  "created_by": "user-1",
  "updated_by_type": "USER",
  "updated_by": "user-1",
  "created_at": "2026-08-12T07:37:38Z",
  "updated_at": "2026-08-12T07:37:53Z"
}
```

Failure shape when present:

```json
{
  "failure": {
    "type": "RUNTIME_UNAVAILABLE",
    "message": "The retained Runtime Target is temporarily unavailable."
  }
}
```

Rationale:

- An Attempt is immutable execution-history evidence for one run or resumed run.
- `runtime` identifies the concrete target and runtime-native session used by this Attempt.
- `lease` is Executor worker ownership metadata, separate from domain execution state.
- `failure` uses the same shape as Execution because Attempt already stores both failure type and
  message.
- `recovery` records what retry and Runtime session cleanup policy resulted from this Attempt.

Accepted decisions:

1. Keep `execution_id` top-level even though the list route is already scoped by Execution, so an
   Attempt remains self-identifying when embedded in Trace or logged independently.
2. Keep `attempt_number` top-level as the user-facing chronological ordinal; `attempt_id` remains
   the stable technical identifier.
3. Always return `lease` with nullable fields after completion, rather than return `lease: null`.
4. Keep `steps` embedded in each Attempt because Step Attempt results are part of immutable Attempt
   history, even though this can make list items large.

## 6. Execution Step Attempt response

Status: `ACCEPTED`

Scope:

- Step Attempt items embedded in Execution Attempt responses for REST and MCP.
- A Step Attempt is immutable evidence of one Step's actual result in one Attempt.

Draft shape:

```json
{
  "step_attempt_id": "f8257d90-5244-41c9-b26e-1f59c658f15e",
  "execution_step_id": "20f8a8d3-1f57-4ff8-9a9d-5bf58e7f30fb",
  "sequence": 0,
  "tool": {
    "skill_name": "data_load",
    "tool_name": "load_data",
    "input_parameters": {}
  },
  "result": {
    "status": "SUCCEEDED",
    "outputs": [],
    "error_message": null
  },
  "lifecycle": {
    "started_at": "2026-08-12T07:37:38Z",
    "finished_at": "2026-08-12T07:37:42Z"
  },
  "created_by_type": "USER",
  "created_by": "user-1",
  "updated_by_type": "USER",
  "updated_by": "user-1",
  "created_at": "2026-08-12T07:37:38Z",
  "updated_at": "2026-08-12T07:37:42Z"
}
```

Rationale:

- `execution_step_id` links immutable Attempt evidence to the current Execution Step definition.
- The parent Execution Attempt already supplies `attempt_id`, so duplicating
  `execution_attempt_id` inside every embedded Step Attempt is unnecessary in this response shape.
- `tool`, `result`, `lifecycle`, and top-level audit fields follow the accepted Execution Step
  response conventions.

Accepted decisions:

1. Keep `execution_step_id` top-level so the Attempt result can be joined back to the logical Step.
2. Omit `execution_attempt_id` because Step Attempts are currently only returned embedded beneath
   an Attempt; add it later if a standalone Step Attempt endpoint is introduced.
3. Do not include `plan` because plan references can be obtained through `execution_step_id`, and
   duplicating mutable descriptive data in immutable evidence is unnecessary.
4. Keep the same `result.error_message` approach as Execution Step because Step Attempt also lacks
   a structured failure type.

## 7. Execution Event response

Status: `ACCEPTED`

Scope:

- REST Execution Event list items.
- MCP Execution Event list items.
- Events are PostgreSQL Transactional Outbox records and expose their Redis publication state.

Draft shape:

```json
{
  "event_id": "caac52f4-c5dd-4f96-99ea-a562d1898dd8",
  "event_type": "execution.completed",
  "payload": {
    "execution_id": "460fbba5-4ad5-46ba-82b0-63899a074816",
    "status": "SUCCEEDED"
  },
  "delivery": {
    "status": "PUBLISHED",
    "attempt_count": 1,
    "available_at": "2026-08-12T07:37:42Z",
    "published_at": "2026-08-12T07:37:42Z",
    "last_error": null
  },
  "created_by_type": "USER",
  "created_by": "user-1",
  "updated_by_type": "USER",
  "updated_by": "user-1",
  "created_at": "2026-08-12T07:37:42Z",
  "updated_at": "2026-08-12T07:37:42Z"
}
```

Rationale:

- `event_type` and `payload` describe the domain event.
- `delivery` describes Transactional Outbox scheduling and Redis publication, which can change
  independently from the immutable event meaning.
- Common audit fields stay at the top level.

Accepted decisions:

1. Keep `event_type` and `payload` top-level rather than wrap them in an `event` object; the
   response is already an Event resource.
2. Rename current `delivery_status` to `delivery.status` and `publish_attempt_count` to
   `delivery.attempt_count`.
3. Keep `available_at` in `delivery`: it is the next eligible Outbox publication time, not an
   Event lifecycle timestamp.
4. Always return `delivery`, with `published_at=null` before successful Redis publication and
   `last_error` containing only the latest safe publication error.

## 8. Execution Artifact response

Status: `ACCEPTED`

Scope:

- REST Artifact list/detail responses.
- MCP Artifact list/detail responses.
- Artifact registration and Agent Asset promotion contracts are outside this response review.

Draft shape:

```json
{
  "artifact_id": "c0798f52-f72c-4f19-92ba-aec55ca71d71",
  "name": "result.png",
  "description": null,
  "type": "PLOT",
  "status": "AVAILABLE",
  "produced_by": {
    "execution_id": "460fbba5-4ad5-46ba-82b0-63899a074816",
    "execution_attempt_id": "b24447fc-a3d7-4d20-ad7d-2882f288ca09",
    "execution_step_id": "20f8a8d3-1f57-4ff8-9a9d-5bf58e7f30fb",
    "execution_step_attempt_id": "f8257d90-5244-41c9-b26e-1f59c658f15e"
  },
  "lineage": {
    "parent_artifact_id": null,
    "external_parent_asset_id": null
  },
  "storage": {
    "type": "PV",
    "uri": "pv:///users/user-1/.../artifacts/plots/result.png",
    "relative_path": "artifacts/plots/result.png",
    "media_type": "image/png",
    "size_bytes": 1024,
    "checksum_sha256": "..."
  },
  "metadata": {},
  "created_by_type": "USER",
  "created_by": "user-1",
  "updated_by_type": "USER",
  "updated_by": "user-1",
  "created_at": "2026-08-12T07:37:42Z",
  "updated_at": "2026-08-12T07:37:42Z"
}
```

Consistent decisions applied without separate review:

- Keep primary identity, display fields, type, status, metadata, and common audit fields at the
  top level.
- Group producer references under `produced_by`.
- Group direct parent references under `lineage`.
- Group physical storage and file-integrity fields under `storage`.
- Always return the nested objects with nullable optional values.

Special decision required:

The current lineage contract supports exactly one direct parent, represented by one of two
mutually exclusive references:

- `parent_artifact_id`: an Executor-owned Artifact.
- `external_parent_asset_id`: an Agent/API-owned Asset.

This can remain as two nullable named fields, or be normalized to a typed reference such as:

```json
{
  "lineage": {
    "parent": {
      "type": "ARTIFACT",
      "id": "c0798f52-f72c-4f19-92ba-aec55ca71d71"
    }
  }
}
```

Accepted decision:

- Keep the named-field form with nullable `parent_artifact_id` and
  `external_parent_asset_id`. It mirrors current persistence, keeps service ownership explicit,
  and avoids prematurely fixing a generalized parent contract while Asset promotion and sharing
  policy remains deferred.

## 9. Execution Trace response semantics

Status: `ACCEPTED: REMOVE`

Scope:

- REST `GET /api/v1/executions/{execution_id}/trace`.
- MCP `execution_trace_get`.

Current behavior:

- Returns the full current Execution object.
- Returns only the first page of Attempts using the default limit of 100.
- Returns only the first page of Events using the default limit of 200.
- Returns only the first page of Artifacts using the default limit of 500.
- Each nested page already carries its own next cursor when more data exists.

Consistent response shape:

```json
{
  "execution": {},
  "attempts": {
    "items": [],
    "next_cursor": null,
    "has_more": false
  },
  "events": {
    "items": [],
    "next_cursor": "opaque-cursor",
    "has_more": true
  },
  "artifacts": {
    "items": [],
    "next_cursor": null,
    "has_more": false
  }
}
```

Accepted decision:

- Remove REST `GET /api/v1/executions/{execution_id}/trace`.
- Remove MCP `execution_trace_get`.
- Remove the transport response models and the application/query composition used only by this
  endpoint.
- Do not replace it with an overview or snapshot endpoint in the current scope.
- Clients obtain current state, Attempts, Events, and Artifacts through their dedicated APIs and
  follow each opaque cursor when complete history is required.

Rationale:

- The endpoint only composes existing APIs and adds no authoritative data.
- Its nested limits mean it cannot guarantee a complete trace despite its name.
- Multi-day Executions can make an unbounded alternative excessively large for REST and MCP.
- Explicit independent queries let UI and Agent clients fetch only the sections they need.

## 10. Capabilities response

Status: `ACCEPTED: REMOVE`

Scope:

- REST `GET /api/v1/capabilities`.
- MCP `executor_get_capabilities`.

Draft shared shape:

```json
{
  "service": "executor-service",
  "protocol": {
    "rest_api_version": "v1",
    "mcp_endpoint": "/mcp",
    "mcp_revision": "2026-07-28",
    "mcp_tasks_supported": false
  },
  "execution": {
    "modes": ["STATIC", "DYNAMIC"],
    "code_source_types": ["INLINE", "PATH"],
    "failure_types": [],
    "retry_strategies": []
  },
  "runtime": {
    "types": ["JUPYTER"],
    "profiles": {
      "JUPYTER": ["basic", "ml"]
    },
    "pools": ["INTERACTIVE", "BATCH"]
  },
  "events": {
    "delivery": "redis-streams-via-transactional-outbox"
  }
}
```

MCP adds its callable Tool names:

```json
{
  "tools": ["execution_submit", "execution_get"]
}
```

Consistent decisions applied without separate review:

- Group protocol, execution, runtime, and event capabilities by concern.
- Keep `service` top-level.
- Do not add audit fields because capabilities are configuration, not a persistent object.
- Remove `execution_trace_get` from MCP Tool capabilities together with the removed Trace Tool.

Accepted decision:

- Remove REST `GET /api/v1/capabilities`.
- Remove MCP `executor_get_capabilities`.
- Remove their transport schemas, static Tool inventories, tests, examples, README and API
  documentation references.
- Do not add a replacement Runtime Profile endpoint in the current scope.
- REST clients use OpenAPI/Docs/ReDoc for route and schema discovery.
- MCP clients use the protocol-standard `tools/list` discovery.
- Clients use Runtime Target list/detail for actual server profile availability, and Execution
  submission validation remains the authoritative global profile acceptance check.

Rationale:

- Most fields duplicate OpenAPI, MCP Tool discovery, enum schemas, or Runtime Target state.
- A manually maintained static inventory can drift from the actual registered routes and Tools.
- Actual schedulability depends on current Runtime Target state and cannot be represented by a
  static capabilities response.

## 11. Pagination conventions

Status: `ACCEPTED`

Scope:

- Runtime Target, Execution, Step, Attempt, Event, and Artifact list responses.
- REST and MCP list Tool cursor contracts.

Accepted baseline rules applied consistently:

- Use opaque cursor pagination; clients never parse or modify a cursor.
- Requests use `cursor` and `limit`.
- REST and custom MCP Tool responses both use `items`, `next_cursor`, and `has_more` through the
  same shared Pydantic page contract.
- A missing next page uses `next_cursor=null` and `has_more=false`.
- Filters must remain unchanged while following a cursor.
- Keep endpoint-specific maximum limits because Event and Artifact payload sizes differ from simple
  object lists.

Accepted decision:

- Remove MCP's silent `max/min` limit clamping.
- Declare minimum and maximum limits in MCP Tool input schemas and reject out-of-range values as
  Tool validation errors.
- Use ranges 1–200 for Runtime Target, Execution, Step, and Attempt lists; 1–500 for Events; and
  1–1000 for Artifacts.
- Do not use a `nextCursor` alias in custom MCP Tool payloads. MCP protocol-native list operations
  use `nextCursor`, but Executor's `execution_list`, `runtime_target_list`, and similar operations
  are custom Tools whose `structuredContent` follows their declared output schema. The official
  SDK still handles all MCP protocol envelopes and discovery.

## 12. Request contracts and structural issues

Status: `ACCEPTED`

Consistent decisions applied without separate review:

- Keep Execution submit fields `mode`, `trigger_type`, `runtime_type`, and `runtime_profile`
  top-level. Response grouping does not require making command requests deeply nested.
- Keep `source`, `context`, and `actor` as their existing explicit request objects.
- Keep mutation `idempotency_key` and `actor` request fields.
- Keep strict `extra=forbid` request validation.
- Keep credentials write-only and excluded from every response.

Special decisions required:

### 12.1 Runtime Target probe idempotency

Status: `ACCEPTED`

Probe mutates persisted health, supported profiles, session count, resource observation, audit
updates, and potentially target scheduling state, but its request currently has only `actor` and no
`idempotency_key`.

Revised recommendation: do not add an `idempotency_key`. Probe is an explicit refresh operation
whose purpose is to observe external state at call time. Reusing an idempotency key must not return
an earlier observation or suppress a new network probe. Repeated calls are safe because probe does
not create a new domain object and only replaces the target's latest observed state and audit
timestamps. Reserve command idempotency keys for intent mutations such as submit, cancel, retry,
disable, activate, and purge.

### 12.2 REST soft-delete route

Status: `ACCEPTED`

Current REST uses `DELETE /runtime-targets/{target_id}` with a JSON body containing
`idempotency_key` and `actor`. Although FastAPI accepts it, some clients, gateways, and generated
SDKs handle DELETE bodies inconsistently.

Recommended decision: replace it with
`POST /runtime-targets/{target_id}/disable`. Keep hard purge as the explicit separate
`POST /runtime-targets/{target_id}/purge` operation. MCP Tool naming can remain
`runtime_target_remove` or be renamed to `runtime_target_disable` for transport consistency.

Accepted naming:

- REST: `POST /api/v1/runtime-targets/{target_id}/disable`.
- MCP: `runtime_target_disable`.
- Remove the old REST DELETE route and MCP `runtime_target_remove`; do not retain aliases.
- `disable` preserves registry and execution history while preventing new assignment.
- `drain`, `activate`, and `purge` remain distinct lifecycle operations.

### 12.3 Runtime Pool MCP query

Status: `ACCEPTED`

REST exposes Runtime Pool aggregate state, but MCP currently has no equivalent Tool.

Recommended decision: add `runtime_pool_list` to MCP using the accepted Runtime Pool response
shape. An Agent choosing a pool normally sends `trigger_type`, not a specific target, but aggregate
pool availability is useful for operational reasoning and parity with REST.

Accepted decision superseding the initial recommendation:

- Keep REST `GET /api/v1/runtime-pools` for operational UI and administration.
- Do not add an MCP `runtime_pool_list` Tool.
- The Agent selects `trigger_type` and `runtime_profile`; Executor exclusively owns pool routing,
  target selection, resource admission, queuing, and reassignment.
- Pool saturation does not prevent submission: the Execution remains durably queued until capacity
  becomes available.

### 12.4 Public error codes

Status: `ACCEPTED`

REST error `code` currently uses Python exception class names such as
`UnsupportedRuntimeProfileError`. These names are implementation details and can change during
refactoring.

Recommended decision: introduce stable uppercase public codes such as
`UNSUPPORTED_RUNTIME_PROFILE`, `EXECUTION_NOT_FOUND`, and `IDEMPOTENCY_CONFLICT`, while retaining
human-readable `message` and validation `details`.

MCP Tool errors should use the same public code and message in their structured or textual error
representation where supported.

Accepted contract:

- Define stable uppercase public codes independently from Python exception class names.
- Domain errors carry or map to a shared public `ErrorCode`.
- REST returns `error.code`, a safe human-readable `error.message`, and validation `details` when
  applicable.
- MCP Tool errors use the official SDK error envelope and include the same stable code and safe
  message, using a textual `[ERROR_CODE] message` representation where no structured application
  error field is available.
- Unexpected failures use `INTERNAL_ERROR` with a generic external message. Tracebacks, raw
  database errors, credentials, and other sensitive diagnostics remain server-log only.

Initial public codes:

- `EXECUTION_NOT_FOUND`
- `ARTIFACT_NOT_FOUND`
- `RUNTIME_TARGET_NOT_FOUND`
- `INVALID_EXECUTION_SPEC`
- `INVALID_CURSOR`
- `UNSUPPORTED_RUNTIME_PROFILE`
- `RUNTIME_TARGET_CONFIGURATION_ERROR`
- `ARTIFACT_REGISTRATION_ERROR`
- `INVALID_STATE_TRANSITION`
- `EXECUTION_VERSION_CONFLICT`
- `IDEMPOTENCY_CONFLICT`
- `PERSISTENCE_CONFLICT`
- `RUNTIME_TARGET_PURGE_CONFLICT`
- `INTERNAL_ERROR`

### 12.5 Shared REST/MCP Pydantic contracts

Status: `ACCEPTED`

Use the same Pydantic response models for FastAPI `response_model` and MCP Tool return annotations.
Each shared model owns one `from_domain()` or `from_view()` mapping path, eliminating the existing
REST/MCP mapping drift (`step_id` vs `id`, omitted fields, and inconsistent page fields).

The official MCP SDK remains responsible for:

- JSON-RPC and Streamable HTTP protocol handling;
- `CallToolResult`, `structuredContent`, and `isError` envelopes;
- Tool `outputSchema` generation from the shared Pydantic return annotation;
- MCP `tools/list` discovery and protocol-native pagination fields.

Transport-specific code remains only where semantics differ:

- REST path/query parameters, HTTP statuses, and HTTP error responses;
- MCP Tool argument wrappers and Tool error conversion;
- request wrappers where REST obtains an identifier from the URL path but MCP requires it in Tool
  arguments.

Shared request components such as `ActorInput`, Execution context, and code source types are also
reused. Deeply nested command request shapes are not introduced merely to match grouped responses.
