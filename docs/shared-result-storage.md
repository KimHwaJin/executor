# Shared execution result storage

Status: accepted implementation contract.

## Ground rules

- PostgreSQL is authoritative for execution state, lineage, fencing, bounded summaries, and the
  canonical result reference.
- Redis Streams carries bounded wake-up events and result references, never complete output
  bodies.
- The Agent and Executor share one filesystem volume. Immutable executable source snapshots and
  complete Step output bodies live on that volume.
- Jupyter is an execution Runtime. Its shared volume contains the user-facing notebook and
  Runtime-produced Artifacts, but no Executor Output Journal or duplicate result metadata files.
- The execution notebook is a retryable projection of sealed shared-volume results. It is not the
  orchestration result store.
- Public contracts expose safe relative paths. Absolute host or container paths are never stored
  in events or returned as result references.

## Shared volume layout

```text
<shared-root>/
├── requests/
└── executions/<execution-id>/
    ├── manifest.json
    ├── sources/<step-id>/source.py
    └── operations/<operation-id>/
        ├── manifest.json
        └── steps/<step-id>/attempts/<attempt-id>/<fencing-token>/
            ├── source.py
            ├── outputs/
            │   └── <ordinal>-<kind>-<representation>.<extension>
            └── manifest.json
```

Writers use a sibling `<fencing-token>.partial` directory and atomically rename it only after all
content and the terminal manifest have been fsynced. A partial directory is never authoritative.

## Result reference

```json
{
  "scope": "STEP",
  "storage": "SHARED_PV",
  "relative_path": "executions/.../manifest.json",
  "checksum_sha256": "hex",
  "attempt_id": "uuid",
  "fencing_token": 3,
  "complete": true,
  "representation_count": 2,
  "total_size_bytes": 1024
}
```

The Agent resolves `relative_path` below its configured shared root and verifies the manifest
checksum before reading declared files. The LLM is not given a general filesystem tool.
`complete=false` means the Runtime stream ended through timeout, cancellation, or an output
message safety limit. Already committed representations remain immutable evidence, but callers
must not interpret them as the complete Step result.

Each representation records its native file path, MIME type, encoding, exact byte size, checksum,
`complete=true`, and `truncated_in_preview=false`. Executor does not silently truncate retained
content.

## Commit ordering

1. Seal the result directory on shared storage.
2. Verify the terminal manifest and checksum.
3. Under the active PostgreSQL execution lease, select the canonical result and update bounded
   summaries.
4. Insert the Agent-facing event in the transactional Outbox.
5. Publish the committed Outbox event to Redis.
6. Project the sealed result into the Jupyter notebook. Projection state moves from `NOT_STARTED`
   to `PENDING`, then `SUCCEEDED` or `FAILED`. Failure is recorded and retried without reversing a
   successful code execution.

An older fencing generation may leave an orphan directory, but it cannot update canonical
PostgreSQL state, emit an authoritative event, or replace a newer notebook cell.

## Query contract

- List operations return state and bounded summaries only.
- Detail operations return state, bounded summaries, and logical result references.
- Consolidated results return lineage and result references, not output bodies.
- Notebook reads remain available for people and UI clients.
- Agent orchestration reads sealed shared-volume files instead of repeatedly calling output-content
  APIs.

## Artifact boundary

This contract moves executable source and cell output. Runtime-produced datasets, plots, models,
reports, logs, and the execution notebook remain on Jupyter-owned storage. Moving or promoting
those Artifacts is a separate policy decision.
