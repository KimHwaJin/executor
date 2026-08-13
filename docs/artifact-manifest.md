# Artifact Manifest contract

Executor automatically discovers files written under an execution's `artifacts/` directory. The
standard execution-scoped layout is:

```text
artifacts/
├── datasets/
├── plots/
├── models/
├── metrics/
├── reports/
├── logs/
└── other/
```

Every directory is created before execution and may contain deeper subdirectories. The first
directory below `artifacts/` determines the automatic Artifact type. Files directly below
`artifacts/` or inside an unknown directory are not registered; producers must use one of the
defined directories and put unknown types under `artifacts/other/`. The final notebook remains
under `notebooks/` and is registered separately.

A Tool only needs the Manifest when it creates a result elsewhere on Jupyter shared storage or records an
S3 object.

The Manifest is an append-only JSON Lines file at:

```text
artifacts/manifest.jsonl
```

Each Step should append complete lines before returning. Executor reads only lines appended during
that Step and links them to the current Execution, Attempt, Step, Skill, and Tool.

## PV entry

```json
{
  "storage_type": "PV",
  "artifact_type": "DATASET",
  "path": "/workspace/pv/users/user-1/datasets/processed/asset-1/data.parquet",
  "name": "daily-processed-data",
  "description": "Preprocessed daily manufacturing data",
  "external_parent_asset_id": "raw-daily-asset-id",
  "metadata": {
    "rows": 120000,
    "columns": 48
  }
}
```

PV paths are interpreted and verified by the Jupyter Runtime storage extension. They may be:

- absolute Jupyter paths under `/workspace/pv`;
- root-relative paths beginning with `users/`;
- paths relative to the current execution workspace.

Symlinks and normalized paths are resolved before validation. Anything outside the configured PV
root is rejected. Jupyter reads the file and computes its actual byte size, MIME type, and SHA-256
checksum; Executor persists the returned metadata and never opens the PV file locally.

## S3 entry

```json
{
  "storage_type": "S3",
  "artifact_type": "MODEL",
  "uri": "s3://analysis-results/models/model.onnx",
  "name": "defect-model",
  "size_bytes": 5821932,
  "checksum_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "parent_artifact_id": "f75bca1a-9d7a-46bd-980a-3517b89c6542"
}
```

S3 entries require `size_bytes` and a 64-character SHA-256 checksum. Credentials, query strings,
and fragments are forbidden in the URI. These values are marked `manifest-declared`; verification
against S3 belongs to a later storage adapter or the Agent Asset service.

## Fields

| Field | Required | Description |
|---|---:|---|
| `storage_type` | yes | `PV` or `S3` |
| `artifact_type` | yes | `DATASET`, `NOTEBOOK`, `REPORT`, `PLOT`, `MODEL`, `METRIC`, `LOG`, `OTHER` |
| `path` | PV | PV file path; mutually exclusive with `uri` |
| `uri` | S3 | `s3://bucket/key`; mutually exclusive with `path` |
| `name` | no | User-facing name; defaults to the filename |
| `description` | no | Description up to 4,000 characters |
| `media_type` | no | Explicit MIME type; inferred for PV when omitted |
| `size_bytes` | S3 | Non-negative object size |
| `checksum_sha256` | S3 | Lower/uppercase SHA-256 hex; normalized to lowercase |
| `parent_artifact_id` | no | Direct parent Execution Artifact |
| `external_parent_asset_id` | no | Parent Asset owned by the Agent/API service |
| `metadata` | no | Additional JSON metadata; secret-shaped keys are redacted before storage |

Only one direct parent is supported initially. Use `parent_artifact_id` when the parent is already
an Executor Artifact and `external_parent_asset_id` for the Agent's global Asset catalog.

## Ownership boundary

`ExecutionArtifact` is immutable execution evidence owned by Executor. The Agent/API service owns
the user-facing `Asset` catalog, reuse scope, naming changes, tags, downloads, and deletion policy.
The promotion and sharing contract is intentionally deferred; see
[Deferred Decisions](deferred-decisions.md). Executor and Agent/API do not share database tables.
