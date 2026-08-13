# Event Delivery and Recovery

PostgreSQL is the Execution state source of truth. Redis Streams is an at-least-once wake-up and
notification channel populated through the Transactional Outbox. This document defines how an
Executor replica acknowledges, reclaims, and quarantines Stream messages.

## Message contract

Every Executor-produced Stream entry contains:

- `event_id`: UUID of the PostgreSQL Outbox Event and consumer deduplication key
- `event_type`: `execution.*` command or notification name
- `schema_version`: event contract version; every event currently uses `1.0`
- `aggregate_type`: `Execution`
- `aggregate_id`: Executor-owned Execution UUID
- `occurred_at`: Outbox creation timestamp
- `payload`: compact event JSON for downstream consumers
- optional `traceparent` and `tracestate`: W3C trace propagation fields

The decoded `payload` is a JSON object that also contains `schema_version` and `execution_id`.
The payload version must equal the Stream field and its `execution_id` must equal `aggregate_id`.
Executor validates this contract both before Outbox persistence and again immediately before Redis
publication. See [Execution Event Contract v1](execution-events-v1.md) for every event payload.

The Executor Worker dispatches only `execution.submitted`, `execution.continue_requested`,
`execution.finish_requested`, `execution.retry_requested`, and `execution.cancel_requested`.
Other valid `execution.*` notifications belong to Agent/frontend consumers and are acknowledged by
the Executor group without starting a job.

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

Messages with missing/invalid UUID routing fields, a non-`Execution` aggregate, a non-
`execution.*` event family, an unsupported schema version, or an invalid v1 payload are copied to
`REDIS_DEAD_LETTER_STREAM` and then ACKed from the primary group. DLQ entries contain only source
Stream/message IDs, UUIDs that passed validation, a fixed reason code, and timestamp. They
deliberately exclude unvalidated routing text, payload, trace headers, code, outputs, and secrets.

There is no automatic DLQ replay. PostgreSQL reconciliation already recovers valid Executor work.
For producer defects, fix the producer and create a new durable command rather than copying an
untrusted DLQ entry back to the primary Stream.

## Retention boundary

Executor does not trim the primary Stream. Agent-owned consumer groups may be slower or may retain
Pending entries, so safe trimming requires a shared retention agreement across all groups.
Published Outbox rows are also retained because `execution_event_list` uses them as the durable
frontend event timeline.
