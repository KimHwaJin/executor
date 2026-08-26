# Agent Execution Event Consumer Guide

이 문서는 Executor에 Execution을 제출한 Agent가 `executor.events`를 안전하게 소비하고,
필요한 경우 PostgreSQL 기반 이벤트 이력으로 누락 구간을 복구하는 방법을 정의한다.
Redis 처리 세부사항은 LLM이나 LangGraph 업무 노드가 아니라 Agent 공통 Event Subscriber가
담당해야 한다.

## 1. 책임 경계

- Executor는 Execution별 `event_sequence`를 PostgreSQL 트랜잭션에서 `1`부터 발급한다.
- Executor Outbox Publisher는 같은 Execution의 앞 순번이 발행되기 전에는 뒤 순번을
  발행하지 않는다.
- 서로 다른 Execution의 이벤트는 병렬로 발행될 수 있다.
- Redis Streams 전달은 at-least-once이므로 중복 전달은 정상 상황이다.
- Agent Subscriber는 Execution별 마지막 연속 처리 순번을 영속적으로 저장한다.
- Agent 그래프는 정렬·중복 제거·누락 복구가 끝난 이벤트만 받는다.

권장 Agent 소유 checkpoint는 다음과 같다.

```json
{
  "execution_id": "7eab4cd7-e124-4ee2-a888-cfc69f7cb298",
  "last_event_sequence": 4
}
```

이 checkpoint와 이벤트가 유발하는 Agent 상태 변경은 가능한 한 같은 DB 트랜잭션으로
저장한다. Redis `XACK`는 두 저장이 성공한 뒤 수행한다.

## 2. Redis 이벤트 Envelope

모든 공개 Execution 이벤트는 다음 일곱 필드만 최상위에 갖는다.

```json
{
  "event_id": "ea3b4285-baf4-4559-9609-e80e9674fe43",
  "event_type": "execution.step_completed",
  "schema_version": "1.0",
  "execution_id": "7eab4cd7-e124-4ee2-a888-cfc69f7cb298",
  "event_sequence": 4,
  "payload": {},
  "occurred_at": "2026-08-26T10:15:31.523Z"
}
```

- `event_id`: 전달 중복 제거 키
- `event_type`: 공개 lifecycle 이벤트 이름
- `schema_version`: 현재 고정값 `1.0`
- `execution_id`: 이벤트가 속한 Executor Execution ID
- `event_sequence`: 해당 Execution 안에서만 증가하는 논리 순번
- `payload`: 이벤트 타입별 데이터
- `occurred_at`: 이벤트가 PostgreSQL `execution_events`에 기록된 시각

`event_sequence`는 Redis Stream message ID, Step의 `sequence`, Attempt 번호,
`occurred_at`과 서로 다른 값이다. 이벤트 순서 판단에는 오직
`execution_id + event_sequence`를 사용한다.

이벤트 타입별 payload는
[Redis Execution Event Contract 1.0](./redis-execution-events.md)을 따른다.

## 3. Agent Subscriber 처리 알고리즘

Execution의 저장된 `last_event_sequence`를 `last`, 수신한 순번을 `received`라 한다.

### 정상 순번: `received == last + 1`

1. 이벤트의 `schema_version`과 payload를 검증한다.
2. Agent 상태 또는 그래프 checkpoint에 이벤트를 적용한다.
3. `last_event_sequence = received`를 저장한다.
4. Redis 메시지를 ACK한다.

### 중복 또는 늦은 이벤트: `received <= last`

이미 처리했거나 DB 복구로 먼저 적용한 이벤트다. 부수효과를 다시 만들지 말고 Redis
메시지만 ACK한다. `event_id`도 별도 deduplication key로 저장하는 것을 권장한다.

### 누락 감지: `received > last + 1`

수신한 이벤트를 먼저 적용하거나 ACK하지 않는다. 다음 이력 조회로 `last` 이후 이벤트를
가져와 연속 순서대로 적용한다.

REST:

```http
GET /api/v1/executions/{execution_id}/events?after_sequence={last}&limit=500
```

MCP:

```text
execution_event_list(
  execution_id="...",
  after_sequence=last,
  limit=500
)
```

응답이 여러 페이지면 `next_cursor`를 다음 요청의 `cursor`에 전달한다. 각 이벤트를
순서대로 적용하면서 checkpoint를 갱신한다. 최초 Redis 이벤트까지 이미 이력에서
처리했다면 그 Redis 메시지는 중복으로 ACK한다.

예를 들어 `last=2`인데 Redis에서 `event_sequence=4`를 받았다면 이력 API에서 3과 4를
가져와 순서대로 적용한다. 이후 늦게 도착한 Redis 3과 현재 Redis 4는 모두 중복 ACK한다.
정상적으로 1, 2, 3, 4가 이어지면 이력 API를 호출하지 않는다.

## 4. 권장 Subscriber 인터페이스

Agent 업무 그래프에는 Redis 메시지 대신 다음처럼 정규화된 결과만 전달한다.

```python
class ExecutionEventSubscriber:
    async def wait_until(
        self,
        execution_id: UUID,
        *,
        after_sequence: int,
        boundary_event_types: set[str],
    ) -> EventBatch:
        """정렬·중복 제거·누락 복구된 연속 이벤트와 wake event를 반환한다."""
```

Subscriber 내부 의사코드는 다음과 같다.

```python
while True:
    event = await read_from_redis_group(execution_id)
    last = await checkpoint.get(execution_id)

    if event.event_sequence <= last:
        await ack(event)
        continue

    if event.event_sequence > last + 1:
        history = await list_events_after(execution_id, last)
        for recovered in contiguous(history, start=last + 1):
            await apply_and_checkpoint(recovered)

    if event.event_sequence == await checkpoint.get(execution_id) + 1:
        await apply_and_checkpoint(event)

    await ack(event)
    if event.event_type in boundary_event_types:
        return accumulated_batch
```

테스트 Agent의 참고 구현은
`test_harness/agent/src/executor_test_agent/integrations/events.py`에 있다.

## 5. SINGLE 실행 흐름

1. `POST /api/v1/executions`로 제출하고 `execution_id`를 저장한다.
2. Subscriber가 다음 lifecycle 이벤트를 연속 순서로 처리한다.
3. `execution.completed`를 wake 경계로 Agent 그래프를 재개한다.
4. Step 결과는 각 `execution.step_completed.payload.result_ref`가 가리키는 공유 PV의
   Manifest와 출력 파일에서 읽는다.
5. 최종 리포트 전 필요하면 `GET /api/v1/executions/{execution_id}/result`로 전체 상태를
   한 번 대조한다.

대표 이벤트 흐름:

```text
execution.started
execution.operation_started
execution.step_started
execution.step_completed
... 반복 ...
execution.operation_completed
execution.completed
```

## 6. MULTI 실행 흐름

1. 최초 Operation을 포함해 Execution을 제출한다.
2. `execution.operation_completed`를 wake 경계로 Agent 그래프를 재개한다.
3. `continuation.allowed=true`이면 Step 결과를 읽고 다음 계획을 만든다.
4. `POST /api/v1/executions/{execution_id}/operations`로 다음 Operation을 추가한다.
5. 더 수행할 Operation이 없으면 `POST /api/v1/executions/{execution_id}/finalize`를 호출한다.
6. 최종 `execution.completed`를 처리한 뒤 결과 정합성을 대조하고 리포트를 작성한다.

Operation 실패라도 `continuation.allowed=true`이면 실패 결과를 바탕으로 보정 Operation을
추가할 수 있다. 이때 `execution.completed`가 오기 전까지 Execution은 종료된 것이 아니다.

## 7. 결과 읽기 원칙

- Redis에는 전체 코드, 전체 텍스트 출력, 이미지 Base64를 넣지 않는다.
- `execution.step_completed`의 `result_ref.relative_path`를 공유 PV 루트에 안전하게
  결합한다.
- Manifest의 checksum, 크기 및 `complete=true`를 검증한 뒤 표현 파일을 읽는다.
- 텍스트와 이미지는 결과 파일에서 읽고, Redis는 결과가 준비됐음을 알리는 wake-up으로
  사용한다.
- 개별 Step 이벤트를 모두 처리했다면 `execution.operation_completed.step_results` 때문에
  추가 API를 호출할 필요는 없다.
- 전체 정합성이 의심되거나 Agent가 오래 중단되었다가 복구되면 Result API를 사용한다.

## 8. 금지사항과 장애 처리

- Redis Stream ID 또는 시각으로 lifecycle 순서를 추론하지 않는다.
- 이벤트 처리 전에 ACK하지 않는다.
- LLM 프롬프트에 Redis consumer group, ACK, gap recovery를 직접 맡기지 않는다.
- 알 수 없는 `schema_version`이나 `event_type`을 조용히 폐기하지 않는다.
- 이벤트 이력 조회 실패 시 뒤 이벤트를 먼저 적용하지 않는다. 메시지를 Pending으로 남겨
  재시도하거나 Agent 오류 채널에 기록한다.
- 한 Execution의 처리만 직렬화한다. 서로 다른 Execution은 병렬 처리해도 된다.

## 9. 구현 체크리스트

- Agent 전용 Redis consumer group을 사용한다.
- `execution_id + last_event_sequence` checkpoint를 영속화한다.
- `event_id` 기반 부수효과 중복 제거를 적용한다.
- 정상 순번에서는 이력 API를 호출하지 않는다.
- gap에서만 `after_sequence`와 cursor 페이지네이션을 사용한다.
- Redis 처리, Agent 상태 저장, ACK의 장애 경계를 테스트한다.
- Subscriber 재시작 뒤 Pending reclaim과 중복 이벤트를 테스트한다.
- SINGLE의 `execution.completed`, MULTI의 `operation_completed`와 `execution.completed` wake
  경계를 테스트한다.
