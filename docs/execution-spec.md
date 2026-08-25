# ExecutionSpec 1.0

ExecutionSpec is the transport-neutral Operation payload shared by REST and MCP. The contract
version remains `1.0` throughout pre-release development. Agent-owned Plan IDs are deliberately
excluded; the Agent stores the Executor-generated `execution_id`, `operation_id`, and `step_id`
receipts beside its own Task, ExecutionPlan, and PlanStep records.

```json
{
  "schema_version": "1.0",
  "steps": [
    {
      "sequence": 0,
      "payload": {
        "type": "PYTHON_EXECUTE",
        "source": {
          "type": "INLINE",
          "content": "result = load_data(product='A')"
        }
      },
      "step_timeout_seconds": 300,
      "lineage": {
        "skill_name": "data_load",
        "tool_name": "load_data",
        "input_parameters": {"product": "A"}
      }
    },
    {
      "sequence": 1,
      "payload": {
        "type": "PYTHON_EXECUTE",
        "source": {
          "type": "PATH",
          "path": "task-100/step-1.py",
          "sha256": "<64 lowercase hex characters>"
        }
      }
    }
  ]
}
```

Required fields are `schema_version`, `steps`, each Step `sequence`, `payload.type`, and
`payload.source`. The current payload type is `PYTHON_EXECUTE`. Each Step independently chooses:

- `INLINE`: UTF-8 Python source is carried in `content`.
- `PATH`: a relative `.py` file under `SHARED_STORAGE_ROOT/requests` is referenced by `path` and required
  SHA-256. The whole ExecutionSpec is never loaded from one PATH file.

`step_timeout_seconds` and `lineage` are optional. Sequences are ordered and contiguous. Initial
submit begins at `0`; each later MULTI Operation starts at the next unused sequence.

Executor validates input-root boundaries, `.py` type, size, checksum, UTF-8, nonblank content,
schema, ordering, and unknown fields before durable state is created. The resolved code and its
source provenance are stored on each ExecutionStep. Runtime notebooks and outputs are written
through the selected Runtime Driver to Runtime-owned storage; input files are never modified.
