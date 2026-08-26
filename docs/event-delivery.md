# Event Delivery and Recovery

PostgreSQL is the Execution state source of truth. Redis Streams is an at-least-once wake-up and
notification channel populated through the Transactional Outbox. This document defines how an
Executor replica acknowledges, reclaims, and quarantines internal work messages, while Agent
consumers independently process integration events.

## Stream boundary

- `executor.work`: internal commands consumed only by the shared `executor-workers` group
- `executor.events`: integration events consumed by an Agent-owned consumer group
- `executor.work.dlq`: invalid internal work-message metadata
- `executor.events.dlq`: reserved for the Agent consumer's invalid integration-event policy

The two primary Streams must never share a consumer group. PostgreSQL and the Transactional
Outbox are the source of truth; Redis is not used as an Execution cache.

## Integration event contract

Every Executor-produced Stream entry contains:

- `event_id`: UUID of the PostgreSQL Outbox Event and consumer deduplication key
- `event_type`: one of the six public Execution lifecycle event names
- `schema_version`: event contract version; every event currently uses `1.0`
- `execution_id`: Executor-owned Execution UUID
- `event_sequence`: monotonic sequence scoped to one Execution
- `occurred_at`: Outbox creation timestamp
- `payload`: compact event JSON for downstream consumers

The decoded `payload` is a JSON object and does not duplicate envelope fields. Executor validates
this contract both before Outbox persistence and again immediately before Redis publication to
`executor.events`. Trace context remains internal to Outbox publishing and Phoenix spans. See
[Redis Execution Event Contract 1.0](../dev_docs/redis-execution-events.md).

The sequence is allocated in the same PostgreSQL transaction as the event. A Publisher does not
select a later event for one Execution while an earlier sequence remains unpublished. Different
Executions continue publishing concurrently. Consumers still validate the sequence because
at-least-once redelivery, process crashes, and parallel handlers can produce duplicates or
out-of-order application completion.

Agent integration code persists the last contiguous sequence per Execution. A gap is recovered
with `execution_event_list` or
`GET /api/v1/executions/{execution_id}/events?after_sequence={last}` before the later Redis event
is applied. Normal contiguous delivery performs no recovery query.

## Internal work contract

Internal entries use `message_id`, `message_type`, the same aggregate and trace fields, and a strict
versioned payload. Supported message types are `operation.ready`,
`execution.finalization_ready`, `execution.retry_ready`, and
`execution.cancellation_ready`. The Worker never reads or acknowledges `executor.events`.

## ACK and duplicate rules

New messages and reclaimed messages use the same processing path:

1. Validate bounded routing metadata.
2. Dispatch a supported command or intentionally ignore a valid notification.
3. ACK only after the routing action succeeds.

If a process dies before step 3, its entry remains in the consumer group's Pending Entries List.
Another live replica reclaims it with `XAUTOCLAIM`. Duplicate delivery is expected: the in-process
job map suppresses a concurrent duplicate in one replica, and PostgreSQL row/state guards allow
only one Worker to create the active Attempt across replicas. Reconciliation independently scans
`QUEUED` and `CANCEL_REQUESTED` rows, so correctness never depends on a Redis entry surviving.

## Pending recovery settings

- `EXECUTION_PENDING_CLAIM_INTERVAL_SECONDS`: how often each Worker scans for stale Pending entries
- `EXECUTION_PENDING_CLAIM_IDLE_MILLISECONDS`: minimum idle time before ownership may move
- `EXECUTION_PENDING_CLAIM_BATCH_SIZE`: maximum entries reclaimed per scan

The claim cursor is retained between scans so a large PEL can be traversed without repeatedly
examining only its first segment. A handler or ACK failure leaves the entry Pending for a later
claim rather than discarding it.

## Dead-letter stream

Work messages with missing/invalid UUID routing fields, a non-`Execution` aggregate, an unsupported
message type/schema version, or an invalid payload are copied to
`REDIS_WORK_DEAD_LETTER_STREAM` and then ACKed from the Worker group. DLQ entries contain only source
Stream/message IDs, UUIDs that passed validation, a fixed reason code, and timestamp. They
deliberately exclude unvalidated routing text, payload, trace headers, code, outputs, and secrets.

There is no automatic DLQ replay. PostgreSQL reconciliation already recovers valid Executor work.
For producer defects, fix the producer and create a new durable command rather than copying an
untrusted DLQ entry back to the primary Stream.

## Retention boundary

Executor does not trim either primary Stream. Each Stream needs a retention policy based on its own
consumer group's delivered and Pending positions.
Published Outbox rows are also retained because `execution_event_list` uses them as the durable
frontend event timeline.
