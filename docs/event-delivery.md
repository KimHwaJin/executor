# Event Delivery and Work Admission

PostgreSQL is the Execution state source of truth. Redis carries only Agent-facing event
Streams, populated through the Transactional Outbox: `executor.events`, and
`executor.events.{tenant_id}` for Executions submitted with a `tenant_id`. Workers do not use Redis to find
work; they poll PostgreSQL.

## Stream boundary

- `executor.events`: integration events consumed by an Agent-owned consumer group
- `executor.events.dlq`: reserved for the Agent consumer's invalid integration-event policy

Redis is not used as an Execution cache.

## Integration event contract

Every Executor-produced Stream entry contains:

- `event_id`: UUID of the durable PostgreSQL Execution Event and consumer deduplication key
- `event_type`: one of the six public Execution lifecycle event names
- `schema_version`: event contract version; every event currently uses `1.0`
- `execution_id`: Executor-owned Execution UUID
- `event_sequence`: monotonic sequence scoped to one Execution
- `occurred_at`: durable Execution Event creation timestamp
- `payload`: compact event JSON for downstream consumers

The decoded `payload` is a JSON object and does not duplicate envelope fields. Executor validates
this contract before durable event/Outbox persistence and again immediately before Redis publication to
`executor.events`. See
[Redis Execution Event Contract 1.0](../dev_docs/redis-execution-events.md).

The sequence is allocated in the same PostgreSQL transaction as the event. A Publisher does not
select a later event for one Execution while an earlier sequence remains unpublished. Different
Executions continue publishing concurrently. Consumers still validate the sequence because
at-least-once redelivery, process crashes, and parallel handlers can produce duplicates or
out-of-order application completion.

The Publisher's predecessor lookup is supported by the partial PostgreSQL index
`ix_outbox_pending_event_order`. It contains only pending Agent-facing events, so long published
histories are not rescanned while selecting the next sequence. Changes to this query or index must
pass `scripts/postgres_query_plan_smoke.py` and the ordered Outbox load gate documented in
`docs/quality-gates.md`.

한 Publisher는 선택한 Execution의 연속된 available 이벤트를
`OUTBOX_BATCH_SIZE` 범위에서 같은 트랜잭션으로 잠그고 순서대로 발행한다. 선행 이벤트의
검증 또는 Redis 발행이 실패하면 같은 Execution의 나머지 이벤트는 그 배치에서 건너뛰며,
실패 이벤트가 재시도되어 성공하기 전까지 다음 Publisher 조회에서도 차단된다.

Agent integration code persists the last contiguous sequence per Execution. A gap is recovered
with `execution_event_list` or
`GET /api/v1/executions/{execution_id}/events?after_sequence={last}` before the later Redis event
is applied. Normal contiguous delivery performs no recovery query.

## Work admission

Every Worker polls PostgreSQL once a second:

1. Every `CANCEL_REQUESTED` Execution and every running Step released for handover is dispatched.
   These need no new Runtime capacity.
2. `QUEUED` and `FINALIZING` Executions are dispatched only up to the Worker's free slots
   (`EXECUTION_WORKER_MAX_ACTIVE` minus its active jobs), oldest first. A cursor rotates through
   the backlog so Executions that cannot be placed do not starve later ones.

The claim locks the Execution row with `FOR UPDATE SKIP LOCKED`, so Workers never queue behind each
other on one row; the in-process job map suppresses a duplicate in one Worker, and the row and
state guards allow only one Worker to create the active Attempt. A backlog therefore costs each Pod
at most its free slots in claims per second, and new work spreads across replicas.

Earlier releases also woke Workers through an `executor.work` Stream. Migration `0010` deleted the
Outbox rows they queued and reduced the Outbox to public events only. The old `executor.work` and
`executor.work.dlq` Redis keys are no longer read and can be deleted.

## Retention boundary

Event retention runs on one Pod per `EVENT_RETENTION_INTERVAL_SECONDS` (default 600), claimed
through the PostgreSQL `event_retention_lease` table. A pass deletes in batches of
`EVENT_RETENTION_BATCH_SIZE`, each in its own transaction, until a batch comes back short or the
pass has run 60 seconds; the rest waits for the next pass. Redis trimming starts once the Redis
compatibility check passes.

- `executor.events` and every tenant Stream are time-trimmed after
  `REDIS_EVENT_RETENTION_SECONDS`. An Agent offline beyond
  that window recovers from durable PostgreSQL history by `event_sequence`.
- `executor.events.dlq` remains Agent-owned and is never modified by Executor.
- Only `PUBLISHED` Outbox rows older than `PUBLISHED_OUTBOX_RETENTION_SECONDS` are deleted.
  Pending and failed publication state is retained.
- Durable `execution_events` are deleted only after the Execution is terminal, its configured
  history window elapsed, and no Outbox row still references the event.

Defaults are Agent events 7 days, published Outbox 7 days, and terminal Execution event history
90 days. They are configurable operational defaults rather than protocol guarantees.
