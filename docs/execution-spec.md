# ExecutionSpec v1

Executor accepts one versioned execution payload through either an INLINE MCP value or a UTF-8
JSON file on the shared PV. Both forms resolve to the same contract:

```json
{
  "schema_version": "1.0",
  "execution_plan_id": "plan-001",
  "steps": [
    {
      "sequence": 0,
      "plan_step_id": "plan-step-001",
      "skill_name": "data_load",
      "tool_name": "load_data",
      "input_parameters": {"product": "A"},
      "code": "df = load_data(product='A')"
    }
  ]
}
```

`schema_version`, `execution_plan_id`, `steps`, every Step `sequence`, `plan_step_id`, and `code`
are required. Sequences are contiguous from zero, PlanStep IDs are unique inside a spec, and blank
code is rejected. Unknown fields are rejected. Skill and Tool names remain optional while the
catalog is being finalized.

## INLINE

```json
{
  "type": "INLINE",
  "spec": {
    "schema_version": "1.0",
    "execution_plan_id": "plan-001",
    "steps": [
      {
        "sequence": 0,
        "plan_step_id": "plan-step-001",
        "code": "print('hello')"
      }
    ]
  }
}
```

The serialized spec must not exceed `EXECUTION_INLINE_SPEC_MAX_BYTES` (256 KiB by default).

## PATH

```json
{
  "type": "PATH",
  "path": "users/user-1/projects/project-1/sessions/session-1/plans/plan-001/source.json",
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

The path is relative to `WORKSPACE_HOST_ROOT`. Absolute paths and paths that resolve outside that
root are rejected. Executor reads at most `EXECUTION_FILE_SPEC_MAX_BYTES` (50 MiB by default),
verifies SHA-256 before parsing, validates the same v1 schema, and persists normalized content in
PostgreSQL. Agent writers must publish files atomically and must not modify a submitted file.

## Materialization

Executor maps every Step to one ExecutionStep and one Jupyter code cell. It writes the accepted
normalized source to `code/execution-spec.json` and the executed cells and outputs to
`notebooks/execution.ipynb`. The Agent owns Task, ExecutionPlan, and PlanStep; Executor stores their
IDs as external references without cross-service foreign keys.

STATIC submit contains every Step. DYNAMIC submit and every `execution_continue` source contain
exactly one Step. A dynamic continuation may refer to a newer ExecutionPlan, but its sequence must
be the next consecutive ExecutionStep sequence.
