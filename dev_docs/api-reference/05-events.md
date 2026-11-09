# 이벤트 스펙(Redis Streams)

Executor는 실행 상태가 바뀔 때 Redis Stream에 이벤트를 발행한다. **이벤트는 깨우는 신호이고 상태 원본은 PostgreSQL(조회 API)이다.** 이벤트 종류는 **8개**, 스키마 버전 `1.0`.

## Stream

- 이름: `{REDIS_EVENT_STREAM}.{tenant_id}`, 기본 `executor.events.{tenant_id}`. tenant마다 하나이고, 한 Execution의 이벤트는 끝까지 같은 Stream으로 간다.
- 전달 보장: at-least-once. 같은 `event_id`가 두 번 이상 올 수 있다.
- 순서: Execution별 `event_sequence`(1부터 1씩 증가)로 판단한다. Redis message ID와 `event_id`는 다른 값이다. Publisher는 앞 순번이 발행될 때까지 뒤 순번을 보류한다.
- 보관: `REDIS_EVENT_RETENTION_SECONDS`(기본 7일) 시간 정리. `REDIS_EVENT_STREAM_MAXLEN`을 설정하면 `XADD ... MAXLEN ~ N`이 붙는다(Redis 6.0 호환, `MINID` 미사용).
- 상태 변경·영구 이벤트·Outbox 행은 한 트랜잭션으로 저장되고, Outbox Publisher가 커밋된 이벤트를 발행한다. 같은 이벤트는 `GET /api/v1/executions/{id}/events`로 다시 읽을 수 있다.

## Stream 항목(필드)

Redis Stream 항목의 필드는 모두 문자열이다. `payload`는 JSON 문자열, `event_sequence`는 숫자 문자열이다.

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `event_id` | string(uuid) | 이벤트 ID(UUID). 중복 제거 키 |
| `event_type` | `execution.started` \| `execution.operation_started` \| `execution.step_started` \| `execution.step_handover_started` \| `execution.step_handover_completed` \| `execution.step_completed` \| `execution.operation_completed` \| `execution.completed` | 아래 8종 중 하나 |
| `schema_version` | `"1.0"` | `1.0` |
| `execution_id` | string(uuid) | Execution ID |
| `event_sequence` | integer | Execution별 순번(1부터) |
| `payload` | object | 유형별 payload(JSON 문자열) |
| `occurred_at` | string(date-time) | 발생 시각(ISO-8601) |
| `tenant_id` | string | Stream의 tenant. payload에는 tenant가 없다 |

## 이벤트 종류

| # | event_type | 언제 |
| ---: | --- | --- |
| 1 | [`execution.started`](#executionstarted) | Worker가 처음 실행을 맡고 커널을 준비했을 때 한 번. retry에서는 다시 나가지 않는다. |
| 2 | [`execution.operation_started`](#executionoperation-started) | Operation 실행 시작. MULTI는 Operation마다, retry에서는 다시 나가지 않는다. |
| 3 | [`execution.step_started`](#executionstep-started) | Step 실행 시작(Attempt마다). |
| 4 | [`execution.step_handover_started`](#executionstep-handover-started) | Worker 종료·무응답으로 실행 중 Step의 관찰이 끊겼다. 커널은 계속 돈다. 상태가 바뀐 것은 아니다. |
| 5 | [`execution.step_handover_completed`](#executionstep-handover-completed) | 다른 Worker가 Step 출력 journal을 복원하고 이어서 관찰한다. |
| 6 | [`execution.step_completed`](#executionstep-completed) | Step 종료. `SUCCEEDED`면 `result_ref`(complete=true)와 `output_summary`가 반드시 있고 `error`는 없다. 실패·취소면 `error`가 있다. 시작 전 취소된 Step(`SKIPPED`)에는 나가지 않는다. |
| 7 | [`execution.operation_completed`](#executionoperation-completed) | Operation 종료. MULTI에서 `execution_status=WAITING_FOR_OPERATION`이면 `continuation`이 있고, Operation이 실패해도 보정 Operation을 붙일 수 있다. |
| 8 | [`execution.completed`](#executioncompleted) | Execution 종료(마지막 이벤트). `SUCCEEDED`면 `retry`·`error`가 없고, 실패·취소면 `error`가 있다. `retry`는 FAILED이고 재시도 가능할 때만. |

<a id="executionstarted"></a>
### 1. `execution.started`

Worker가 처음 실행을 맡고 커널을 준비했을 때 한 번. retry에서는 다시 나가지 않는다.

| payload 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `status` | `"RUNNING"` | O |  | 이 이벤트 시점의 상태 |
| `runtime` | RuntimeSummary | O |  | 할당된 Runtime |
| `runtime.provider` | string | O | 길이≥1, 길이≤64 | Runtime 종류(`JUPYTER`) |
| `runtime.profile` | string | O | 길이≥1, 길이≤128 | 커널 프로파일 |
| `runtime.target_id` | string(uuid) | O |  | Runtime Target ID |
| `runtime.kernel_id` | string \| null |  | 길이≤255 | Jupyter 커널 ID(호출자 `X-Session-Id`와 무관) |

<a id="executionoperation-started"></a>
### 2. `execution.operation_started`

Operation 실행 시작. MULTI는 Operation마다, retry에서는 다시 나가지 않는다.

| payload 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `status` | `"RUNNING"` | O |  | 이 이벤트 시점의 상태 |
| `operation` | StartedOperationReference | O |  | Operation 참조 |
| `operation.id` | string(uuid) | O |  | ID |
| `operation.number` | integer | O | ≥1 | 순번(1부터) |
| `operation.step_count` | integer | O | ≥1 | 이 Operation의 Step 수 |

<a id="executionstep-started"></a>
### 3. `execution.step_started`

Step 실행 시작(Attempt마다).

| payload 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `status` | `"RUNNING"` | O |  | 이 이벤트 시점의 상태 |
| `operation` | OperationReference | O |  | Operation 참조 |
| `operation.id` | string(uuid) | O |  | ID |
| `operation.number` | integer | O | ≥1 | 순번(1부터) |
| `step` | StepReference | O |  | Step 참조 |
| `step.id` | string(uuid) | O |  | ID |
| `step.sequence` | integer | O | ≥0 | Step 순번 |
| `attempt` | AttemptReference | O |  | Attempt 참조 |
| `attempt.id` | string(uuid) | O |  | ID |
| `attempt.number` | integer | O | ≥1 | 순번(1부터) |
| `attempt.reason` | `INITIAL` \| `RETRY` | O |  | 사유 |

<a id="executionstep-handover-started"></a>
### 4. `execution.step_handover_started`

Worker 종료·무응답으로 실행 중 Step의 관찰이 끊겼다. 커널은 계속 돈다. 상태가 바뀐 것은 아니다.

| payload 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `status` | `"RUNNING"` | O |  | 이 이벤트 시점의 상태 |
| `operation` | OperationReference | O |  | Operation 참조 |
| `operation.id` | string(uuid) | O |  | ID |
| `operation.number` | integer | O | ≥1 | 순번(1부터) |
| `step` | StepReference | O |  | Step 참조 |
| `step.id` | string(uuid) | O |  | ID |
| `step.sequence` | integer | O | ≥0 | Step 순번 |
| `attempt` | AttemptReference | O |  | Attempt 참조 |
| `attempt.id` | string(uuid) | O |  | ID |
| `attempt.number` | integer | O | ≥1 | 순번(1부터) |
| `attempt.reason` | `INITIAL` \| `RETRY` | O |  | 사유 |
| `reason` | `EXECUTOR_SHUTDOWN` \| `EXECUTOR_UNRESPONSIVE` | O |  | 사유 |
| `kernel_running` | `true` | O |  | 커널은 계속 실행 중(`true` 고정) |
| `detected_at` | string(date-time) | O |  | 감지 시각 |

<a id="executionstep-handover-completed"></a>
### 5. `execution.step_handover_completed`

다른 Worker가 Step 출력 journal을 복원하고 이어서 관찰한다.

| payload 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `status` | `"RUNNING"` | O |  | 이 이벤트 시점의 상태 |
| `operation` | OperationReference | O |  | Operation 참조 |
| `operation.id` | string(uuid) | O |  | ID |
| `operation.number` | integer | O | ≥1 | 순번(1부터) |
| `step` | StepReference | O |  | Step 참조 |
| `step.id` | string(uuid) | O |  | ID |
| `step.sequence` | integer | O | ≥0 | Step 순번 |
| `attempt` | AttemptReference | O |  | Attempt 참조 |
| `attempt.id` | string(uuid) | O |  | ID |
| `attempt.number` | integer | O | ≥1 | 순번(1부터) |
| `attempt.reason` | `INITIAL` \| `RETRY` | O |  | 사유 |
| `paused_seconds` | number | O | ≥0 | 관찰이 끊긴 시간(초) |
| `outputs_complete` | `true` | O |  | 인계 중 출력이 빠짐없이 복원됨(`true` 고정) |
| `finished_during_handover` | boolean | O |  | 인계 중에 Step이 끝났는지 |

<a id="executionstep-completed"></a>
### 6. `execution.step_completed`

Step 종료. `SUCCEEDED`면 `result_ref`(complete=true)와 `output_summary`가 반드시 있고 `error`는 없다. 실패·취소면 `error`가 있다. 시작 전 취소된 Step(`SKIPPED`)에는 나가지 않는다.

| payload 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `status` | `SUCCEEDED` \| `FAILED` \| `CANCELLED` | O |  | 이 이벤트 시점의 상태 |
| `operation` | OperationReference | O |  | Operation 참조 |
| `operation.id` | string(uuid) | O |  | ID |
| `operation.number` | integer | O | ≥1 | 순번(1부터) |
| `step` | StepReference | O |  | Step 참조 |
| `step.id` | string(uuid) | O |  | ID |
| `step.sequence` | integer | O | ≥0 | Step 순번 |
| `attempt` | AttemptReference | O |  | Attempt 참조 |
| `attempt.id` | string(uuid) | O |  | ID |
| `attempt.number` | integer | O | ≥1 | 순번(1부터) |
| `attempt.reason` | `INITIAL` \| `RETRY` | O |  | 사유 |
| `result_ref` | ResultReference \| null | O |  | 공유 PV의 Step 결과 manifest 참조 |
| `result_ref.storage` | `"SHARED_PV"` | O |  | `SHARED_PV` |
| `result_ref.relative_path` | string | O | 길이≥1, 길이≤4096 | 공유 루트 기준 상대경로 |
| `result_ref.media_type` | string | O | 길이≥1, 길이≤255 | manifest MIME |
| `result_ref.size_bytes` | integer | O | ≥0 | manifest 크기 |
| `result_ref.checksum_sha256` | string | O | `^[0-9a-f]{64}$` | manifest SHA-256(소문자 hex) |
| `result_ref.complete` | boolean | O |  | 출력이 모두 저장됐는지(실행 성공 여부 아님) |
| `output_summary` | OutputSummary \| null | O |  | 출력 요약 |
| `output_summary.count` | integer | O | ≥0 | 출력 개수 |
| `output_summary.content_types` | array<string> | O |  | 출력 MIME 목록(정렬·중복 제거) |
| `error` | ErrorSummary \| null | O |  | 오류 요약 |
| `error.code` | string | O | 길이≥1, 길이≤128 | 오류 코드 |
| `error.message` | string | O | 길이≥1, 길이≤2000 | 오류 메시지 |
| `error.retryable` | boolean | O |  | 재시도 가능 여부 |

<a id="executionoperation-completed"></a>
### 7. `execution.operation_completed`

Operation 종료. MULTI에서 `execution_status=WAITING_FOR_OPERATION`이면 `continuation`이 있고, Operation이 실패해도 보정 Operation을 붙일 수 있다.

| payload 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `status` | `SUCCEEDED` \| `FAILED` \| `CANCELLED` | O |  | 이 이벤트 시점의 상태 |
| `execution_status` | `WAITING_FOR_OPERATION` \| `SUCCEEDED` \| `FAILED` \| `CANCELLED` | O |  | Operation 종료 직후 Execution 상태 |
| `operation` | OperationReference | O |  | Operation 참조 |
| `operation.id` | string(uuid) | O |  | ID |
| `operation.number` | integer | O | ≥1 | 순번(1부터) |
| `step_summary` | StepSummary | O |  | Step 집계 |
| `step_summary.total` | integer | O | ≥0 | 전체 |
| `step_summary.completed` | integer | O | ≥0 | 끝난 수(성공+실패+취소) |
| `step_summary.succeeded` | integer | O | ≥0 | 성공 |
| `step_summary.failed` | integer | O | ≥0 | 실패 |
| `step_summary.cancelled` | integer | O | ≥0 | 취소 |
| `step_results` | array<StepResult> | O |  | 이 Operation에서 실제 수행된 Step의 최신 결과(sequence 순) |
| `step_results[].step_id` | string(uuid) | O |  | 실패 Step |
| `step_results[].sequence` | integer | O | ≥0 | Step 순번 |
| `step_results[].status` | `SUCCEEDED` \| `FAILED` \| `CANCELLED` | O |  | 이 이벤트 시점의 상태 |
| `step_results[].attempt` | AttemptReference | O |  | Attempt 참조 |
| `step_results[].attempt.id` | string(uuid) | O |  | ID |
| `step_results[].attempt.number` | integer | O | ≥1 | 순번(1부터) |
| `step_results[].attempt.reason` | `INITIAL` \| `RETRY` | O |  | 사유 |
| `step_results[].result_ref` | ResultReference \| null | O |  | 공유 PV의 Step 결과 manifest 참조 |
| `step_results[].result_ref.storage` | `"SHARED_PV"` | O |  | `SHARED_PV` |
| `step_results[].result_ref.relative_path` | string | O | 길이≥1, 길이≤4096 | 공유 루트 기준 상대경로 |
| `step_results[].result_ref.media_type` | string | O | 길이≥1, 길이≤255 | manifest MIME |
| `step_results[].result_ref.size_bytes` | integer | O | ≥0 | manifest 크기 |
| `step_results[].result_ref.checksum_sha256` | string | O | `^[0-9a-f]{64}$` | manifest SHA-256(소문자 hex) |
| `step_results[].result_ref.complete` | boolean | O |  | 출력이 모두 저장됐는지(실행 성공 여부 아님) |
| `continuation` | Continuation \| null | O |  | 다음 Operation 가능 정보(MULTI 대기일 때만) |
| `continuation.allowed` | `true` | O |  | `true` 고정 |
| `continuation.expected_version` | integer | O | ≥0 | 다음 operations·finalize에 보낼 `expected_version` |
| `continuation.expires_at` | string(date-time) | O |  | 기한 |
| `error` | OperationErrorSummary \| null | O |  | 오류 요약 |
| `error.code` | string | O | 길이≥1, 길이≤128 | 오류 코드 |
| `error.message` | string | O | 길이≥1, 길이≤2000 | 오류 메시지 |
| `error.retryable` | boolean | O |  | 재시도 가능 여부 |
| `error.step_id` | string(uuid) \| null |  |  | 실패 Step |

<a id="executioncompleted"></a>
### 8. `execution.completed`

Execution 종료(마지막 이벤트). `SUCCEEDED`면 `retry`·`error`가 없고, 실패·취소면 `error`가 있다. `retry`는 FAILED이고 재시도 가능할 때만.

| payload 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `status` | `SUCCEEDED` \| `FAILED` \| `CANCELLED` | O |  | 이 이벤트 시점의 상태 |
| `finalized_by` | `AGENT` \| `WAIT_TIMEOUT` \| `MAX_RUNTIME` \| null |  |  | 무엇이 끝냈나(`AGENT`/`WAIT_TIMEOUT`/`MAX_RUNTIME`), SINGLE 정상 종료·취소는 `null` |
| `operation_summary` | OperationSummary | O |  | Operation 집계 |
| `operation_summary.total` | integer | O | ≥0 | 전체 |
| `operation_summary.succeeded` | integer | O | ≥0 | 성공 |
| `operation_summary.failed` | integer | O | ≥0 | 실패 |
| `operation_summary.cancelled` | integer | O | ≥0 | 취소 |
| `retry` | RetryAvailability \| null | O |  | 재시도 가능 정보(FAILED이고 가능할 때만) |
| `retry.allowed` | `true` | O |  | `true` 고정 |
| `retry.from_step_id` | string(uuid) | O |  | 재시도가 시작할 Step |
| `retry.expires_at` | string(date-time) | O |  | 기한 |
| `error` | ExecutionErrorSummary \| null | O |  | 오류 요약 |
| `error.code` | string | O | 길이≥1, 길이≤128 | 오류 코드 |
| `error.message` | string | O | 길이≥1, 길이≤2000 | 오류 메시지 |
| `error.retryable` | boolean | O |  | 재시도 가능 여부 |
| `error.step_id` | string(uuid) \| null |  |  | 실패 Step |
| `error.operation_id` | string(uuid) \| null |  |  | 실패 Operation |
| `execution` | ExecutionSummary \| null |  |  | 제출 시 식별 정보(라우팅용) |
| `execution.trigger` | ExecutionTriggerSummary | O |  | 트리거 |
| `execution.trigger.type` | `INTERACTIVE` \| `BATCH` | O |  | `INTERACTIVE`/`BATCH` |
| `execution.context` | ExecutionContextSummary | O |  | 소유 범위 |
| `execution.context.user_id` | string | O | 길이≥1, 길이≤255 | 소유 사용자(제출 때 `X-User-Id`) |
| `execution.context.project_id` | string \| null |  | 길이≤255 | 제출 때 `X-Project-Id` |
| `execution.context.session_id` | string \| null |  | 길이≤255 | 제출 때 `X-Session-Id`(호출자 세션) |
| `execution.metadata` | object |  |  | 제출 최상위 metadata 그대로 |

## 흐름

```text
SINGLE 성공 : started → operation_started → (step_started → step_completed)×N → operation_completed → completed
MULTI       : started → operation_started → … → operation_completed(WAITING_FOR_OPERATION, continuation)
              POST .../operations → operation_started → … → operation_completed(WAITING_FOR_OPERATION)
              POST .../finalize   → completed
SINGLE retry: … → step_completed(FAILED) → operation_completed(FAILED) → completed(FAILED, retry)
              POST .../retry → step_started → step_completed → … → operation_completed → completed
              (retry에서는 started·operation_started가 다시 나가지 않는다)
Worker 인계 : step_started → step_handover_started → step_handover_completed → step_completed
```

## 소비자 규칙

1. 자기 시스템 전용 consumer group을 `0`부터 만든다: `XGROUP CREATE executor.events.{tenant} {group} 0 MKSTREAM`(이미 있으면 `BUSYGROUP` 무시).
2. `XREADGROUP GROUP {group} {consumer} COUNT 100 BLOCK 5000 STREAMS executor.events.{tenant} >`로 읽고, **처리를 마친 뒤에만** `XACK`한다.
3. 처리한 `event_id`를 영속 저장하고 다시 오면 적용하지 않고 `XACK`한다.
4. Execution별 마지막 연속 `event_sequence`(last)를 저장한다. `== last+1`이면 적용, `<= last`면 무시하고 ACK, `> last+1`이면 먼저 `GET /api/v1/executions/{id}/events?after_sequence={last}&limit=500`으로 빈 구간을 채운다.
5. 모르는 `event_type`은 적용하지 않고 기록 후 `XACK`한다. 지원하지 않는 `schema_version`은 적용하지 않는다.
6. Redis 6.0에는 `XAUTOCLAIM`이 없다. `XPENDING`/`XCLAIM`으로 오래 걸린 메시지를 회수하고, 본문이 없는 ID는 `XACK` 후 이력 API로 채운다.
7. 결과 원문은 이벤트에 없다. `result_ref.relative_path`를 공유 루트에 안전하게 결합해 manifest를 읽고 크기·SHA-256을 검증한다.
8. 최종 리포트 전이나 상태가 의심스러우면(순서가 이상함, 집계 불일치, 체크섬 불일치, 상충하는 종료 상태) `GET .../result`로 원본을 확인한다.
9. `CANCELLED`는 취소 확정이지 커널 정리 성공 보장이 아니다. `COMPLETION_FAILED`는 필수 노트북·Artifact 처리 또는 최종 정리 실패다.

## 이벤트에 들어가지 않는 정보

Runtime endpoint·토큰·인증정보, DB·Redis 접속정보, 코드 본문, 전체 출력 텍스트, 이미지 Base64, 전체 traceback, Worker·Lease ID, fencing token.
