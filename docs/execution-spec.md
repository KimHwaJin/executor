# ExecutionSpec 1.0

ExecutionSpec is the transport-neutral source format used by both REST and MCP. It contains only
ordered runtime Steps. Agent-owned plan IDs are deliberately excluded; the Agent binds its Plan to
the Executor-generated `execution_id`, `operation_id`, and `step_id` values returned by a command.

```json
{
  "schema_version": "1.0",
  "steps": [
    {
      "sequence": 0,
      "payload": {
        "type": "CODE",
        "content": "result = load_data(product='A')"
      },
      "step_timeout_seconds": 300,
      "lineage": {
        "skill_name": "data_load",
        "tool_name": "load_data",
        "input_parameters": {"product": "A"}
      }
    }
  ]
}
```

Required fields are `schema_version`, `steps`, each Step `sequence`, and `payload`. The only current
payload variant is `{"type":"CODE","content":"..."}`. `step_timeout_seconds` and `lineage` are
optional. Sequences must be ordered and contiguous. Initial submit starts at `0`; each later MULTI
Operation starts at the next unused sequence.

INLINE embeds this document in the request. PATH identifies a UTF-8 JSON document under
`INPUT_HOST_ROOT` and includes its SHA-256 checksum:

```json
{
  "type": "PATH",
  "path": "requests/task-100/execution-spec.json",
  "sha256": "<64 lowercase hex characters>"
}
```

Executor validates the path boundary, file type, size, hash, schema, sequence order, nonblank code,
and unknown fields before creating durable state. The immutable canonical source and checksum are
stored on the Operation. Runtime notebooks and artifacts are written through the selected Runtime
Driver to Runtime-owned storage, never to the Agent/Executor input directory.
