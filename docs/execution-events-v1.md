# Execution Event Contract v1

Executor records every event in the PostgreSQL Transactional Outbox and publishes it to the
configured Redis Stream with at-least-once delivery. This is the integration contract for Agent,
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

## Command and notification boundary

Executor's own Worker group dispatches these command events:

- `execution.submitted`
- `execution.continue_requested`
- `execution.finish_requested`
- `execution.retry_requested`
- `execution.cancel_requested`

All other valid events are notifications. Executor acknowledges them in its own group without
starting work. The Agent must use a distinct consumer group so its delivery position and Pending
Entries List are independent of Executor Workers.

## Event payloads

Every row below also contains the common `schema_version` and `execution_id` fields. Enum values
use the same uppercase strings returned by the REST and MCP execution APIs.

| Event | Additional required payload fields |
| --- | --- |
| `execution.submitted` | `task_id`, `execution_plan_id`, `status=QUEUED` |
| `execution.continue_requested` | `task_id`, `execution_plan_id`, `plan_step_id`, `status=QUEUED`, `sequence`, `version` |
| `execution.finish_requested` | `task_id`, `execution_plan_id`, `status=QUEUED`, `version` |
| `execution.cancel_requested` | `task_id`, `execution_plan_id`, `status=CANCEL_REQUESTED` |
| `execution.retry_requested` | `task_id`, `execution_plan_id`, `status=QUEUED`, `from_sequence`, `retry_strategy`, nullable `previous_failure_type`, `retry_count` |
| `execution.started` | `status=RUNNING` |
| `execution.resumed` | `status=RUNNING` |
| `execution.retry_deferred` | `status=QUEUED`, `failure_type`, `retry_strategy`, `reason`, `runtime_target_id` |
| `execution.step_completed` | `status=WAITING_FOR_NEXT_STEP`, `execution_attempt_id`, `sequence`, `step_status=SUCCEEDED`, `version` |
| `execution.step_failed` | `status=WAITING_FOR_NEXT_STEP`, `execution_attempt_id`, `sequence`, `step_status=FAILED`, `version` |
| `execution.artifact_registered` | `execution_attempt_id`, `execution_step_id`, `artifact_id`, `artifact_type`, `storage_type`, `status`, `uri` |
| `execution.artifact_failed` | `status=RUNNING`, `execution_attempt_id`, `sequence`, `error_type` |
| `execution.succeeded` | `status=SUCCEEDED`, nullable `failure_type`, `retry_strategy`, nullable `retry_from_sequence`, `runtime_session_cleanup_status`; optional `recovery_count`, `reason` |
| `execution.failed` | same terminal fields with `status=FAILED` |
| `execution.cancelled` | `status=CANCELLED`, `runtime_session_cleanup_status` |
| `execution.timeout_requested` | `status=CANCEL_REQUESTED`, `failure_type=EXECUTION_TIMEOUT` |
| `execution.runtime_session_cleanup_completed` | `status=FAILED`, `runtime_session_cleanup_status=SUCCEEDED` |
| `execution.runtime_session_cleanup_failed` | `status=FAILED`, `runtime_session_cleanup_status=FAILED` |
| `execution.retry_window_expired` | `status=FAILED`, `runtime_session_cleanup_status`, `retry_was_queued` |

Event payloads deliberately exclude generated code, cell outputs, dataset values, credentials,
tokens, and raw exception messages. `execution.artifact_registered.uri` is storage metadata and
must never contain embedded credentials.

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

If step 4 encounters an existing `event_id`, skip the state change and ACK it. If processing fails
before commit or ACK, leave the message Pending and recover it with `XAUTOCLAIM`. Do not use an
in-memory set as the only deduplication store.

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

### v1 rollout

Stream entries published before this contract was introduced have no `schema_version` and are not
v1 messages. At rollout, first reconcile Agent Task state through Executor REST/MCP execution
queries, then create the Agent consumer group at `$`. Do not replay a mixed legacy/v1 Stream from
`0` with a v1-only consumer. The Executor Worker safely dead-letters a legacy or malformed command
that reaches its own group rather than dispatching it.

A legacy PostgreSQL Outbox row still in `PENDING` is validated and normalized to v1 inside the
publisher transaction before its first Redis publication. A payload that cannot satisfy a known
v1 event model remains unpublished for operator inspection rather than being emitted as malformed
data.

## Retention and recovery

Executor does not trim the shared Stream. Retention must consider the delivered and Pending
positions of every consumer group. Agent consumers should monitor `XPENDING`, reclaim stale
messages with `XAUTOCLAIM`, and keep their deduplication records at least as long as an event can be
redelivered. The durable timeline is also available through `execution_event_list` and
`GET /api/v1/executions/{id}/events` for reconciliation.
