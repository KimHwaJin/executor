# Execution Event Contract v1

Executor records every event in the PostgreSQL Transactional Outbox and publishes it to the
configured `executor.events` Redis Stream with at-least-once delivery. This is the integration contract for Agent,
frontend notification, and operational consumers. PostgreSQL remains the source of truth for
Execution state; events are notifications and wake-up signals.

## Stream envelope

Every Redis Stream entry contains string fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `event_id` | UUID | Outbox primary key and consumer deduplication key |
| `event_type` | string | Event name listed below |
| `schema_version` | `1.0` | Stream contract version |
| `aggregate_type` | `Execution` | Aggregate family |
| `aggregate_id` | UUID | Executor-owned `execution_id` |
| `occurred_at` | RFC 3339 timestamp | Time the Outbox row was created |
| `payload` | JSON object encoded as string | Versioned event-specific payload |
| `traceparent` | optional string | W3C parent trace context |
| `tracestate` | optional string | W3C vendor trace context |

Every payload contains:

```json
{
  "schema_version": "1.0",
  "execution_id": "5f0f0934-9803-4c03-8b04-78ce25b738e5"
}
```

`payload.schema_version` must equal the Stream `schema_version`, and `payload.execution_id` must
equal `aggregate_id`. All event models reject unknown fields. Changing required fields or meaning
requires a new schema version and a parallel compatibility period.

## Internal work boundary

Executor's own Worker does not consume this Stream. Durable internal commands are published to
`executor.work` as:

- `operation.ready`
- `execution.finalization_ready`
- `execution.retry_ready`
- `execution.cancellation_ready`

The Agent uses its own consumer group on `executor.events`; no group is shared with Executor Workers.

## Event payloads

Every row below also contains the common `schema_version` and `execution_id` fields. Enum values
use the same uppercase strings returned by the REST and MCP execution APIs.

| Event | Additional required payload fields |
| --- | --- |
| `execution.submitted` | `task_id`, `execution_plan_id`, `operation_id`, `first_sequence`, `last_sequence`, `status=QUEUED` |
| `execution.continue_requested` | same Operation identity and range fields plus `version` |
| `execution.finish_requested` | `task_id`, `execution_plan_id`, `status=QUEUED`, `version` |
| `execution.cancel_requested` | `task_id`, `execution_plan_id`, `status=CANCEL_REQUESTED` |
| `execution.retry_requested` | `task_id`, `execution_plan_id`, `operation_id`, `status=QUEUED`, `from_sequence`, `retry_strategy`, nullable `previous_failure_type`, `retry_count` |
| `execution.started` | `status=RUNNING` |
| `execution.resumed` | `status=RUNNING` |
| `execution.step_started` | `execution_attempt_id`, `operation_id`, `step_id`, `sequence`, `status=RUNNING` |
| `execution.step_succeeded` | same identities, `status=SUCCEEDED`, `result.outputs`, nullable `result.execution_count` |
| `execution.step_failed` | same identities, `status=FAILED`, partial `result.outputs`, nullable `result.execution_count`, `error_message` |
| `execution.retry_deferred` | `status=QUEUED`, `failure_type`, `retry_strategy`, `reason`, `runtime_target_id` |
| `execution.operation_succeeded` | `status`, `execution_attempt_id`, `operation_id`, `operation_status=SUCCEEDED`, `first_sequence`, `last_sequence`, `version` |
| `execution.operation_failed` | same Operation identity/range fields, `operation_status=FAILED`, nullable `execution_attempt_id` and `failed_sequence`, `version` |
| `execution.artifact_registered` | `execution_attempt_id`, `execution_step_id`, `artifact_id`, `artifact_type`, `storage_type`, `status`, `uri` |
| `execution.artifact_failed` | `status=RUNNING`, `execution_attempt_id`, `sequence`, `error_type` |
| `execution.succeeded` | `status=SUCCEEDED`, nullable `failure_type`, `retry_strategy`, nullable `retry_from_sequence`, `runtime_session_cleanup_status`; optional `recovery_count`, `reason` |
| `execution.failed` | same terminal fields with `status=FAILED` |
| `execution.cancelled` | `status=CANCELLED`, `runtime_session_cleanup_status` |
| `execution.timeout_requested` | `status=CANCEL_REQUESTED`, `failure_type=EXECUTION_TIMEOUT` |
| `execution.runtime_session_cleanup_completed` | `status=FAILED`, `runtime_session_cleanup_status=SUCCEEDED` |
| `execution.runtime_session_cleanup_failed` | `status=FAILED`, `runtime_session_cleanup_status=FAILED` |
| `execution.retry_window_expired` | `status=FAILED`, `runtime_session_cleanup_status`, `retry_was_queued` |

Event payloads exclude generated code, dataset values, credentials, and tokens. A Step outcome
includes the Runtime's structured result: Jupyter MIME outputs (including base64 image data when
returned by Jupyter) or the corresponding generic Runtime output. Current operation assumes these
results are bounded; large-result offloading is a deferred extension. Artifact URIs must never
contain embedded credentials.

## Agent consumption and deduplication

Redis can deliver one `event_id` more than once if Executor crashes after `XADD` but before marking
the Outbox row `PUBLISHED`, or if a consumer dies before ACK. Agent handling must therefore be:

1. Read with an Agent-owned consumer group.
2. Parse and validate the v1 envelope.
3. Start an Agent database transaction.
4. Insert `event_id` into a table with a unique or primary-key constraint.
5. Apply the corresponding Agent Task/report/UI state change in the same transaction.
6. Commit the database transaction.
7. ACK the Redis message.

For incremental execution, persist each `execution.step_succeeded` result with the Agent graph
checkpoint. `execution.operation_succeeded` is emitted only after all Step result events for that
Operation, so it can safely wake the Agent to plan the next Operation. If step 4 encounters an
existing `event_id`, skip the state change and ACK it. If processing fails
before commit or ACK, leave the message Pending and recover it with `XAUTOCLAIM`. Do not use an
in-memory set as the only deduplication store.

A STATIC retry reuses the accepted `operation_id` but creates a new Attempt. Therefore the same
Operation may emit more than one terminal event over time, each with a distinct `event_id` and
normally a distinct `execution_attempt_id`. Consumers deduplicate by `event_id`, not by
`operation_id`. `execution_attempt_id` is null only when a queued retry becomes terminal before a
Runtime Attempt can start, such as a disabled retained target or an expired retry window.

Run the reference consumer against a local Executor Stream:

```bash
AGENT_EVENT_CONSUMER_GROUP=agent-execution-events \
  uv run python scripts/agent_event_consumer_example.py
```

The example stores demonstration state in `.agent-event-consumer.db`, which is ignored by Git.
Production Agent code should use its PostgreSQL database. Set
`AGENT_EVENT_STOP_AFTER_TERMINAL=true` for a one-terminal-event demonstration. New groups consume
only events published after group creation (`$`) by default. Set `AGENT_EVENT_GROUP_START_ID=0`
only on a Stream known to contain v1 events exclusively. The example reclaims stale Pending
messages after
`AGENT_EVENT_PENDING_IDLE_MILLISECONDS` (default 30000). It leaves invalid messages Pending;
production should add an Agent-owned DLQ and alert policy.

At rollout, create the Executor Worker group on `executor.work` and the Agent group on
`executor.events`. Do not reuse the former shared Stream/group configuration.

## Retention and recovery

Executor does not trim the shared Stream. Retention must consider the delivered and Pending
positions of every consumer group. Agent consumers should monitor `XPENDING`, reclaim stale
messages with `XAUTOCLAIM`, and keep their deduplication records at least as long as an event can be
redelivered. The durable timeline is also available through `execution_event_list` and
`GET /api/v1/executions/{id}/events` for reconciliation.
