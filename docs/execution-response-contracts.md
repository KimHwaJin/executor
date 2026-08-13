# Execution Response Contracts

Status: `CURRENT`

This document is the source of truth for REST and MCP Execution-family successful responses. REST
response models and MCP structured content share these Pydantic contracts. Every audited resource
returns the complete six-field audit set: `created_by_type`, `created_by`, `created_at`,
`updated_by_type`, `updated_by`, and `updated_at`.

## Commands

`execution_submit`, `execution_cancel`, `execution_retry`, `execution_continue`, and
`execution_finish` return only `execution_id`, `state: {status, version}`, and the audit set.
REST returns `202 Accepted` and a `Location` header for the Execution detail resource. MCP returns
the same structured content without an HTTP-specific Location field. A repeated idempotent command
returns the current persisted status and version, not a hard-coded state.

## Execution reads

- Execution list items contain ID, mode, trigger type, context, state, Step count, lifecycle, and
  the audit set. Runtime, failure, retry, and cancellation detail are excluded.
- Execution detail contains source, Runtime assignment, state, workspace, failure, retry, recovery,
  deadlines, lifecycle, and audit fields. It does not embed Steps.
- Step list and Step detail use the same item shape: Step and Execution IDs, sequence, code hash,
  Plan and Tool references, result, lifecycle, and audit fields. Executable code is excluded.

## Attempt reads

- Attempt list items contain Attempt and Execution IDs, attempt number, state, failure, Step count,
  lifecycle, and audit fields.
- Attempt detail adds Runtime, lease, and recovery fields. It does not embed Step Attempts.
- Attempt Step list items contain Step Attempt and logical Execution Step IDs, sequence, Tool,
  result, lifecycle, and audit fields.
- REST routes are `/executions/{execution_id}/attempts`,
  `/executions/{execution_id}/attempts/{attempt_id}`, and
  `/executions/{execution_id}/attempts/{attempt_id}/steps`.
- MCP Tools are `execution_attempt_list`, `execution_attempt_get`, and
  `execution_attempt_step_list`.
- Attempt and Step Attempt child lookups validate that the Attempt belongs to the path's Execution;
  a mismatch returns `EXECUTION_ATTEMPT_NOT_FOUND`.

## Events and Artifacts

- Event list items retain event type, redacted payload, Outbox delivery state, and audit fields for
  PostgreSQL-to-Redis reconciliation.
- Artifact list items are summaries: identity, name, type, status, producer references, storage
  type/media type/size, and audit fields.
- Artifact detail additionally returns description, lineage, URI, relative path, checksum, and
  metadata.

All child collection operations use opaque cursor pagination and return `items`, `next_cursor`,
and `has_more`.
