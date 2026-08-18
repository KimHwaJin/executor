# Execution Event Contract v2

Executor persists integration events in PostgreSQL Transactional Outbox and publishes them to the
`executor.events` Redis Stream with at-least-once delivery. Events wake Agent/frontend consumers;
PostgreSQL remains authoritative.

Each Stream entry has `event_id`, `event_type`, `schema_version=2.0`,
`aggregate_type=Execution`, `aggregate_id`, `occurred_at`, JSON `payload`, and optional W3C
`traceparent`/`tracestate`. Every payload includes `schema_version=2.0` and `execution_id` matching
the envelope. Unknown fields and unsupported event types are rejected.

Executor Workers consume a separate `executor.work` Stream. Its internal messages are
`operation.ready`, `execution.finalization_ready`, `execution.retry_ready`, and
`execution.cancellation_ready`. Agent and Worker consumer groups never share a Stream.

## Integration events

| Event | Key payload fields |
|---|---|
| `execution.submitted` | `task_id`, `idempotency_key`, `operation_id`, Step receipts, sequence range, `status=QUEUED` |
| `execution.operation_submitted` | same fields plus optimistic `version` |
| `execution.finalization_requested` | `task_id`, `status=FINALIZING`, `version` |
| `execution.cancel_requested` | `task_id`, `status=CANCEL_REQUESTED` |
| `execution.retry_requested` | `task_id`, `operation_id`, retry range/strategy/count |
| `execution.started`, `execution.resumed` | `status=RUNNING` |
| `execution.step_started` | Attempt, Operation, Step IDs, sequence, status |
| `execution.step_succeeded` | identities plus `result.outputs` and `result.execution_count` |
| `execution.step_failed` | identities, partial outputs, and `error_message` |
| `execution.operation_succeeded`, `execution.operation_failed` | Operation outcome, sequence range, version; failure includes `error_message` |
| `execution.waiting_for_operation` | `operation_id`, `operation_wait_expires_at`, version |
| `execution.artifact_registered`, `execution.artifact_failed` | Artifact identity or registration failure |
| `execution.succeeded`, `execution.failed`, `execution.cancelled` | terminal state and cleanup/retry information |
| `execution.timeout_requested` | Execution maximum-runtime cancellation request |
| cleanup/retry-window events | retained-session cleanup outcome |

Step success carries the complete bounded Runtime result. For Jupyter this is the cell MIME output,
including base64 image data when returned by Jupyter. A MULTI Agent can checkpoint each Step result;
the Operation outcome and `execution.waiting_for_operation` arrive after all Step events.

Consumers must deduplicate by `event_id` in durable storage, commit the deduplication record and
business state together, and ACK only after commit. Reclaim stale Pending entries with
`XAUTOCLAIM`. Never deduplicate by Operation ID: a SINGLE retry reuses its Operation but creates a
new Attempt and new events.

The same durable event history is available through `execution_event_list` and
`GET /api/v1/executions/{execution_id}/events`.
