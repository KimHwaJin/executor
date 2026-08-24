# Execution response contracts

REST and MCP share the same successful Pydantic response models. REST-only path identifiers and
MCP-only request wrappers adapt into the same application commands.

All mutation responses contain `execution_id`, `state`, the complete audit field set, and optional
`operation`. Submit, retry, and Operation creation return:

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

Cancel and finalize omit `operation` because they do not create or requeue an Operation.

Execution detail groups information by responsibility: `context`, `runtime`, `workspace`,
`state`, `failure`, `retry`, `recovery`, `deadlines`, and `lifecycle`. The lifecycle object exposes
`operation_mode`, `operation_wait_timeout_seconds`, `started_at`, and `finished_at`. Runtime
assignment fields remain null until the Worker selects a target and opens a session.
`workspace.notebook_projection` independently reports `NOT_STARTED`, `PENDING`, `SUCCEEDED`, or
`FAILED`, its attempt count, last safe error, and projection time. A projection failure does not
change a successful Execution result.

Step detail exposes Executor-owned `step_id`, `sequence`, `code_hash`, per-Step `source` provenance,
`step_timeout_seconds`, `lineage`, `result`, lifecycle timestamps, and audit fields. `result`
contains a bounded `output_summary` and, after success or failure, a canonical `SHARED_PV`
`result_ref` with a safe relative manifest path, checksum, authoritative Attempt, and fencing
token. It never duplicates full output bodies. Operation
detail exposes accepted `schema_version` (currently always `1.0`), its sequence range,
`operation_timeout_seconds`, metadata, result, and audit fields.

`GET /executions/{execution_id}/result` and MCP `execution_result_get` return Execution detail,
every Operation with its current Step results, immutable Attempts with Step Attempts, and full
Artifact metadata in one call. The Operation-scoped equivalents return one Operation and its Steps.
These are the authoritative state and lineage reads after Redis signals
`result_available=true`. The Agent resolves each Step's reference below its configured shared
root, verifies the manifest and declared file checksums, and reads only those representations
needed for reasoning or a report.

```json
{
  "status": "SUCCEEDED",
  "output_summary": {
    "output_count": 2,
    "output_types": {"display_data": 1, "stream": 1},
    "stream_names": ["stdout"],
    "mime_types": ["image/png", "text/plain"],
    "has_image": true,
    "image_count": 1,
    "has_error": false
  },
  "result_ref": {
    "storage": "SHARED_PV",
    "execution_id": "...",
    "step_id": "...",
    "attempt_id": "...",
    "fencing_token": 3,
    "relative_path": "executions/.../manifest.json",
    "checksum_sha256": "hex"
  },
  "error_message": null
}
```

List endpoints use opaque cursor pagination with `items`, `next_cursor`, and `has_more`.
