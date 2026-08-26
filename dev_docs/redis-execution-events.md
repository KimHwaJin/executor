# Executor Redis 실행 이벤트 스키마

## 1. 문서 목적

이 문서는 Agent가 Executor에 Execution을 제출한 뒤 Redis Streams에서 어떤 이벤트를
받고, 각 필드를 어떻게 해석하며, 어느 시점에 Executor API를 다시 조회해야 하는지
정의한다.

현재 이벤트 계약 버전은 `2.0`이다. 이벤트 계약 버전은 Execution 요청 본문의 버전과
별개이며, Redis envelope와 payload 형식을 버전 관리하기 위한 값이다.

계약의 코드 원본은 다음과 같다.

- `src/executor_service/events.py`
- 기본 Event Stream: `executor.events`
- 환경변수: `REDIS_EVENT_STREAM`

## 2. Stream 구분

Executor는 서로 다른 두 Redis Stream을 사용한다.

| Stream | 기본 이름 | 생산자 | 소비자 | 용도 |
|---|---|---|---|---|
| Work Stream | `executor.work` | Executor API/서비스 | Executor Worker | 내부 실행 명령 전달 |
| Event Stream | `executor.events` | Executor Outbox Publisher | Agent, Frontend 등 | 외부 실행 상태 알림 |

Agent는 반드시 `executor.events`만 소비해야 한다. `executor.work`에는
`operation.ready`, `execution.finalization_ready`, `execution.retry_ready`,
`execution.cancellation_ready` 같은 Executor 내부 메시지가 들어가며 외부 계약이 아니다.

각 Agent 서비스는 Executor Worker의 consumer group과 다른, Agent 소유의 consumer
group을 사용해야 한다. 서로 독립적으로 모든 이벤트를 받아야 하는 다른 시스템끼리도
consumer group을 공유하면 안 된다.

## 3. 전달 보장과 원본 데이터

이벤트는 PostgreSQL Transactional Outbox에 Execution 상태 변경과 같은 트랜잭션으로
저장된 뒤 Redis Stream으로 발행된다.

- 전달 방식: at-least-once
- 상태 원본: PostgreSQL
- Redis 역할: 상태 변경을 알리는 wake-up channel
- 중복 가능성: 있음
- 재정렬 가능성: 있음
- 출력 원문 포함 여부: 포함하지 않음

Agent는 이벤트 payload만으로 최종 상태를 확정하지 않고, Operation 또는 Execution
경계 이벤트를 받은 뒤 Executor 조회 API로 PostgreSQL 원본 상태를 확인해야 한다.

## 4. Redis Stream envelope

Redis의 한 Stream entry는 다음 field mapping을 가진다. Redis에서 읽을 때 모든 최상위
값은 문자열이며 `payload`만 JSON 문자열이므로 별도로 JSON object로 파싱해야 한다.

```json
{
  "event_id": "7baf32b7-3665-43f7-bb37-e42e431fe31a",
  "event_type": "execution.step_succeeded",
  "schema_version": "2.0",
  "aggregate_type": "Execution",
  "aggregate_id": "5fa09f84-6b86-4fd2-9ed4-4438def57e28",
  "occurred_at": "2026-08-26T04:15:30.123456+00:00",
  "payload": "{...JSON string...}",
  "traceparent": "00-...-...-01",
  "tracestate": "vendor=value"
}
```

| 필드 | 타입 | 필수 | 의미 |
|---|---|---:|---|
| `event_id` | UUID | O | Outbox 이벤트 식별자. 중복 제거 키 |
| `event_type` | string | O | 이벤트 종류 |
| `schema_version` | `2.0` | O | envelope 스키마 버전 |
| `aggregate_type` | `Execution` | O | 이벤트 aggregate 종류 |
| `aggregate_id` | UUID | O | 대상 `execution_id`와 같은 값 |
| `occurred_at` | ISO 8601 datetime | O | 이벤트가 Outbox에 생성된 시각 |
| `payload` | JSON string | O | 이벤트별 payload |
| `traceparent` | string | X | W3C Trace Context trace parent |
| `tracestate` | string | X | W3C Trace Context vendor state |

Redis가 부여하는 Stream message ID와 `event_id`는 서로 다르다.

- Stream message ID: `XACK`, `XAUTOCLAIM`에 사용한다.
- `event_id`: Agent DB에서 영속적으로 중복 제거할 때 사용한다.

모든 payload에는 다음 공통 필드가 포함된다.

| 필드 | 타입 | 필수 | 규칙 |
|---|---|---:|---|
| `schema_version` | `2.0` | O | envelope의 `schema_version`과 같아야 함 |
| `execution_id` | UUID | O | envelope의 `aggregate_id`와 같아야 함 |

알 수 없는 최상위 필드, 알 수 없는 payload 필드, 지원하지 않는 `event_type`은 현재
계약 검증에서 허용되지 않는다.

## 5. 공통 하위 객체

### 5.1 Step receipt

제출 이벤트의 `steps[]`는 Agent가 보낸 Step과 Executor 발급 ID의 매핑이다.

```json
{
  "sequence": 0,
  "step_id": "2c3da7a9-3f4b-43e2-b295-74b998bc081b"
}
```

| 필드 | 타입 | 의미 |
|---|---|---|
| `sequence` | integer, 0 이상 | Execution 전체 Step 순서 |
| `step_id` | UUID | Executor가 발급한 논리 Step ID |

### 5.2 `result_ref`

`result_ref`는 결과 원문 자체가 아니라 결과를 찾기 위한 참조다.

| 필드 | 타입 | 조건 | 의미 |
|---|---|---|---|
| `scope` | `STEP`, `OPERATION`, `EXECUTION` | 필수 | 결과 참조 범위 |
| `operation_id` | UUID/null | 선택 | 관련 Operation |
| `step_id` | UUID/null | 선택 | 관련 Step |
| `storage` | `SHARED_PV` | STEP 필수 | Step 원본 저장소 |
| `relative_path` | string | STEP 필수 | 공유 PV 루트 기준 `manifest.json` 경로 |
| `checksum_sha256` | 64자리 hex string | STEP 필수 | manifest SHA-256 |
| `execution_attempt_id` | UUID | STEP 필수 | 결과를 만든 Attempt |
| `fencing_token` | integer, 1 이상 | STEP 필수 | 결과를 쓴 Worker lease 세대 |
| `complete` | boolean | STEP 필수 | Step 결과 기록이 정상적으로 봉인됐는지 |

`scope=STEP`이면 공유 PV의 실제 manifest 위치가 들어간다. Agent는 자신의
`SHARED_STORAGE_ROOT`에 `relative_path`를 결합하고 checksum을 검증한 뒤 원본 코드,
텍스트, JSON, HTML, 이미지를 읽는다.

`scope=OPERATION` 또는 `scope=EXECUTION`은 논리적인 결과 경계 참조다. 이때는 공유 PV
경로가 직접 포함되지 않으므로 다음 API를 호출한다.

```http
GET /api/v1/executions/{execution_id}/operations/{operation_id}/result
GET /api/v1/executions/{execution_id}/result
```

### 5.3 `output_summary`

Step 실행 출력의 크기가 아닌 종류와 개수를 빠르게 판단하기 위한 요약이다.

```json
{
  "output_count": 2,
  "output_types": {
    "stream": 1,
    "display_data": 1
  },
  "stream_names": ["stdout"],
  "mime_types": ["image/png", "text/plain"],
  "has_image": true,
  "image_count": 1,
  "has_error": false
}
```

| 필드 | 타입 | 의미 |
|---|---|---|
| `output_count` | integer | Jupyter 호환 output record 개수 |
| `output_types` | object | `stream`, `display_data`, `execute_result`, `error` 등의 개수 |
| `stream_names` | string[] | `stdout`, `stderr` 등 발견된 Stream 이름 |
| `mime_types` | string[] | `text/plain`, `application/json`, `image/png` 등 MIME 목록 |
| `has_image` | boolean | 이미지 MIME representation 존재 여부 |
| `image_count` | integer | 이미지 representation 개수 |
| `has_error` | boolean | `error` output 존재 여부 |

Redis에는 텍스트 전체나 base64 이미지가 들어가지 않는다.

## 6. 이벤트 목록

### 6.1 명령 접수 이벤트

#### `execution.submitted`

최초 Execution과 첫 Operation이 PostgreSQL에 저장되고 내부 실행 요청이 Outbox에 함께
기록된 시점이다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `status` | `QUEUED` | 실행 대기 상태 |
| `task_id` | string | Agent가 제출한 상위 Task ID |
| `idempotency_key` | string | 최초 제출 멱등성 키 |
| `operation_id` | UUID | Executor가 발급한 첫 Operation ID |
| `steps` | Step receipt[] | sequence와 Step ID 매핑 |
| `first_sequence` | integer | Operation 첫 sequence |
| `last_sequence` | integer | Operation 마지막 sequence |

#### `execution.operation_submitted`

MULTI Execution에 후속 Operation이 추가된 시점이다.

`execution.submitted` 필드 전체와 다음 필드를 포함한다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `version` | integer | Operation 추가 후 Execution 상태 버전 |

#### `execution.finalization_requested`

MULTI Execution에 더 이상 Operation을 추가하지 않고 종료하도록 요청한 시점이다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `status` | `FINALIZING` | 최종화 요청 상태 |
| `task_id` | string | 상위 Task ID |
| `version` | integer | 최종화 요청 후 Execution 상태 버전 |

#### `execution.cancel_requested`

취소 요청이 저장된 시점이다. 아직 Runtime 중단과 세션 정리가 끝난 상태는 아니다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `status` | `CANCEL_REQUESTED` | 취소 처리 대기/진행 상태 |
| `task_id` | string | 상위 Task ID |

#### `execution.retry_requested`

실패한 Execution의 명시적 재시도 요청이 저장된 시점이다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `status` | `QUEUED` | 재실행 대기 상태 |
| `task_id` | string | 상위 Task ID |
| `operation_id` | UUID | 다시 실행할 Operation |
| `from_sequence` | integer | 재실행을 시작할 sequence |
| `retry_strategy` | enum | `FROM_FAILED_STEP` 또는 `FROM_START` |
| `previous_failure_type` | enum/null | 직전 실패 분류 |
| `retry_count` | integer, 1 이상 | 누적 재시도 횟수 |

### 6.2 실행 시작 이벤트

#### `execution.started`

Worker가 새로운 Runtime 실행 소유권과 lease를 획득하고 실행을 시작한 시점이다.

| payload 필드 | 타입 | 값 |
|---|---|---|
| `status` | enum | `RUNNING` |

#### `execution.resumed`

보존된 Runtime 세션을 다시 확보하여 후속 Operation 또는 실패 Step 재시도를 계속하는
시점이다.

| payload 필드 | 타입 | 값 |
|---|---|---|
| `status` | enum | `RUNNING` |

### 6.3 Step 이벤트

#### `execution.step_started`

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `execution_attempt_id` | UUID | 현재 실행 Attempt |
| `operation_id` | UUID | Step이 속한 Operation |
| `step_id` | UUID | 논리 Step ID |
| `sequence` | integer | Execution 전체 Step 순서 |
| `status` | `RUNNING` | Step 실행 중 |

#### `execution.step_succeeded`

Step 실행 결과가 공유 PV에 저장되고 DB 참조가 같은 트랜잭션으로 확정된 시점이다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `execution_attempt_id` | UUID | 결과를 생성한 Attempt |
| `operation_id` | UUID | 소속 Operation |
| `step_id` | UUID | 논리 Step ID |
| `sequence` | integer | Step 순서 |
| `status` | `SUCCEEDED` | Step 성공 |
| `result_available` | `true` | 결과 참조 사용 가능 |
| `result_ref` | object | `scope=STEP` 공유 PV manifest 참조 |
| `output_summary` | object | 출력 종류 요약 |
| `execution_count` | integer/null | Runtime이 반환한 실행 순번 |

#### `execution.step_failed`

실패 출력도 공유 PV에 보존한 뒤 발행된다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `execution_attempt_id` | UUID | 실패가 발생한 Attempt |
| `operation_id` | UUID | 소속 Operation |
| `step_id` | UUID | 논리 Step ID |
| `sequence` | integer | 실패한 Step 순서 |
| `status` | `FAILED` | Step 실패 |
| `result_available` | `true` | 실패 출력 참조 사용 가능 |
| `result_ref` | object | `scope=STEP` 공유 PV manifest 참조 |
| `output_summary` | object | 오류 포함 출력 종류 요약 |
| `error_message` | string | 최대 2,000자의 실패 메시지 |

### 6.4 Operation 경계 이벤트

#### `execution.operation_succeeded`

Operation의 모든 Step이 성공한 시점이다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `status` | `WAITING_FOR_OPERATION`, `SUCCEEDED`, `FAILED` | Execution의 현재 상태 |
| `execution_attempt_id` | UUID/null | Operation을 실행한 Attempt |
| `operation_id` | UUID | 완료된 Operation |
| `operation_status` | `SUCCEEDED` | Operation 결과 |
| `first_sequence` | integer | 첫 Step sequence |
| `last_sequence` | integer | 마지막 Step sequence |
| `version` | integer | 경계 확정 후 Execution 버전 |
| `result_available` | `true` | Operation 결과 조회 가능 |
| `result_ref` | object | `scope=OPERATION` 논리 참조 |

#### `execution.operation_failed`

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `status` | `WAITING_FOR_OPERATION`, `SUCCEEDED`, `FAILED` | Execution의 현재 상태 |
| `execution_attempt_id` | UUID/null | Attempt 전에 실패하면 `null` 가능 |
| `operation_id` | UUID | 실패한 Operation |
| `operation_status` | `FAILED` | Operation 결과 |
| `first_sequence` | integer | 첫 Step sequence |
| `last_sequence` | integer | 마지막 Step sequence |
| `version` | integer | 경계 확정 후 Execution 버전 |
| `result_available` | `true` | Operation 결과 조회 가능 |
| `result_ref` | object | `scope=OPERATION` 논리 참조 |
| `failed_sequence` | integer/null | 식별 가능할 때 실패 sequence |
| `error_message` | string | 최대 2,000자의 실패 메시지 |

#### `execution.waiting_for_operation`

MULTI Operation 처리가 끝났고 Agent의 다음 명령을 받을 수 있는 확정 경계다.
`operation_succeeded` 또는 `operation_failed` 다음에 생성된다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `status` | `WAITING_FOR_OPERATION` | 다음 명령 대기 상태 |
| `operation_id` | UUID | 방금 종료된 Operation |
| `operation_wait_expires_at` | datetime | 후속 Operation/finalize 허용 기한 |
| `version` | integer | 다음 요청의 `expected_version`으로 사용할 버전 |

Agent는 이 이벤트를 MULTI 실행의 주 wake-up 이벤트로 사용한다. Operation 결과 API를
조회한 뒤 다음 Operation을 추가하거나 `finalize`를 호출한다.

### 6.5 Artifact 이벤트

#### `execution.artifact_registered`

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `execution_attempt_id` | UUID/null | Artifact 생성 Attempt |
| `execution_step_id` | UUID/null | Artifact 생성 Step |
| `artifact_id` | UUID | Executor Artifact ID |
| `artifact_type` | enum | Artifact 분류 |
| `storage_type` | `PV`, `S3` | 저장소 종류 |
| `status` | enum | `AVAILABLE`, `INCOMPLETE`, `DELETED` |
| `uri` | string | 등록된 Artifact URI |

Artifact type은 `DATASET`, `NOTEBOOK`, `REPORT`, `PLOT`, `MODEL`, `METRIC`, `LOG`,
`OTHER` 중 하나다. 실제 파일을 내려받거나 최신 정보를 확인할 때는 Artifact 상세/다운로드
API를 사용한다.

#### `execution.artifact_failed`

Artifact manifest를 읽거나 등록하는 부가 처리에 실패한 이벤트다. 코드 Step 자체의 성공
여부와 별개일 수 있다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `status` | `RUNNING` | 발생 당시 Execution 상태 |
| `execution_attempt_id` | UUID | 발생 Attempt |
| `sequence` | integer | 관련 Step sequence |
| `error_type` | string | 구조화된 Artifact 오류 종류 |

### 6.6 정상 및 실패 종료 이벤트

#### `execution.succeeded`

Execution 전체가 성공한 terminal 이벤트다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `status` | `SUCCEEDED` | terminal 성공 상태 |
| `failure_type` | null | 성공이므로 일반적으로 `null` |
| `retry_strategy` | enum | 종료 시점 재시도 정책 |
| `retry_from_sequence` | integer/null | 재시도 시작점이 있을 때만 포함 |
| `runtime_session_cleanup_status` | enum | Runtime 세션 정리 결과 |
| `recovery_count` | integer/null | Worker 복구 처리 횟수 |
| `reason` | string/null | 구조화된 종료 사유 |
| `result_available` | `true` | Execution 결과 인덱스 조회 가능 |
| `result_ref` | object | `scope=EXECUTION` 논리 참조 |

#### `execution.failed`

Execution 전체가 실패한 terminal 이벤트다. 실패한 코드와 출력이 보존돼 있으면 Result
API와 Step manifest로 확인할 수 있다.

필드 구조는 `execution.succeeded`와 같지만 다음 값이 달라진다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `status` | `FAILED` | terminal 실패 상태 |
| `failure_type` | enum/null | 분류 가능한 실패 원인 |
| `retry_strategy` | enum | 허용되는 명시적 재시도 방식 |

#### `execution.cancelled`

취소 및 Runtime 정리가 끝난 terminal 이벤트다. 취소 Execution에는 결과를 새로 만들지
않으므로 `result_available`과 `result_ref`가 없다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `status` | `CANCELLED` | terminal 취소 상태 |
| `runtime_session_cleanup_status` | enum | Runtime 세션 정리 결과 |

Terminal event type은 다음 세 개다.

```text
execution.succeeded
execution.failed
execution.cancelled
```

### 6.7 복구·정리·시간제한 이벤트

#### `execution.retry_deferred`

실패 Step부터 재시도하려 했지만 기존 Runtime Target을 일시적으로 사용할 수 없어 재시도가
미뤄진 상태다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `status` | `QUEUED` | 재시도 대기 상태 |
| `failure_type` | enum | 일반적으로 `RUNTIME_UNAVAILABLE` |
| `retry_strategy` | enum | 일반적으로 `FROM_FAILED_STEP` |
| `reason` | string | 지연 사유 코드 |
| `runtime_target_id` | UUID | 복구를 기다리는 Runtime Target |

#### `execution.timeout_requested`

Execution 최대 실행시간이 지나 내부 취소 절차가 요청된 시점이다. 아직 terminal이 아니다.

| payload 필드 | 타입 | 값 |
|---|---|---|
| `status` | `CANCEL_REQUESTED` | 시간초과 정리 진행 상태 |
| `failure_type` | `EXECUTION_TIMEOUT` | 시간초과 분류 |

#### `execution.runtime_abort_started`

Runtime interrupt/delete를 시작한 시점이다.

#### `execution.runtime_abort_completed`

Runtime abort가 확인 가능한 상태로 완료된 시점이다.

#### `execution.runtime_abort_failed`

정해진 시간 안에 Runtime abort를 확인하지 못했거나 정리에 실패한 시점이다.

세 Runtime abort 이벤트는 같은 필드 구조를 사용한다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `status` | Execution status | 발생 당시 Execution 상태 |
| `execution_attempt_id` | UUID | 관련 Attempt |
| `failure_type` | enum | abort를 유발한 실패 분류 |
| `runtime_abort_status` | enum | abort 세부 상태 |
| `runtime_session_cleanup_status` | enum | 세션 정리 상태 |
| `session_reusable` | boolean | 기존 Runtime 세션 재사용 가능 여부 |
| `message` | string/null | 최대 2,000자의 진단 메시지 |
| `version` | integer | 이벤트 발생 후 Execution 상태 버전 |

#### `execution.runtime_session_cleanup_completed`

실패/취소 후 남은 Runtime 세션 정리가 성공한 이벤트다.

| payload 필드 | 타입 | 값 |
|---|---|---|
| `status` | `FAILED`, `CANCELLED` | Execution terminal 상태 |
| `runtime_session_cleanup_status` | `SUCCEEDED` | 정리 성공 |

#### `execution.runtime_session_cleanup_failed`

| payload 필드 | 타입 | 값 |
|---|---|---|
| `status` | `FAILED`, `CANCELLED` | Execution terminal 상태 |
| `runtime_session_cleanup_status` | `FAILED` | 정리 실패 |

#### `execution.retry_window_expired`

보존 Runtime 세션의 재시도 가능 기간이 끝나 세션을 정리하고 Execution 실패를 확정한
시점이다.

| payload 필드 | 타입 | 의미 |
|---|---|---|
| `status` | `FAILED` | 최종 상태 |
| `runtime_session_cleanup_status` | enum | 세션 정리 결과 |
| `retry_was_queued` | boolean | 만료 당시 재시도가 대기 중이었는지 |

## 7. Enum 값

### `failure_type`

```text
TOOL_ERROR
INFRASTRUCTURE_ERROR
WORKER_SHUTDOWN
RUNTIME_UNAVAILABLE
LEASE_EXPIRED
INTERNAL_ERROR
OPERATION_WAIT_TIMEOUT
OPERATION_TIMEOUT
STEP_TIMEOUT
EXECUTION_TIMEOUT
OUTPUT_LIMIT_EXCEEDED
RUNTIME_SESSION_LOST
```

### `retry_strategy`

```text
NOT_RETRYABLE
FROM_FAILED_STEP
FROM_START
```

### `runtime_session_cleanup_status`

```text
NOT_REQUIRED
PENDING
SUCCEEDED
FAILED
```

### `runtime_abort_status`

```text
NOT_REQUIRED
PENDING
IDLE_CONFIRMED
SESSION_DELETED
SESSION_MISSING
FAILED
```

## 8. 대표 이벤트 예시

다음은 Redis `payload` 문자열을 JSON object로 파싱한 뒤의 예시다.

### Step 성공

```json
{
  "schema_version": "2.0",
  "execution_id": "5fa09f84-6b86-4fd2-9ed4-4438def57e28",
  "execution_attempt_id": "ecf3fcde-82c3-454f-8819-1a62e4b59326",
  "operation_id": "3cfbafec-a1cb-4fa0-a8ad-8e61bd43514d",
  "step_id": "2c3da7a9-3f4b-43e2-b295-74b998bc081b",
  "sequence": 0,
  "status": "SUCCEEDED",
  "result_available": true,
  "result_ref": {
    "scope": "STEP",
    "operation_id": "3cfbafec-a1cb-4fa0-a8ad-8e61bd43514d",
    "step_id": "2c3da7a9-3f4b-43e2-b295-74b998bc081b",
    "storage": "SHARED_PV",
    "relative_path": "executions/5fa09f84-6b86-4fd2-9ed4-4438def57e28/operations/3cfbafec-a1cb-4fa0-a8ad-8e61bd43514d/steps/2c3da7a9-3f4b-43e2-b295-74b998bc081b/attempts/ecf3fcde-82c3-454f-8819-1a62e4b59326/1/manifest.json",
    "checksum_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "execution_attempt_id": "ecf3fcde-82c3-454f-8819-1a62e4b59326",
    "fencing_token": 1,
    "complete": true
  },
  "output_summary": {
    "output_count": 1,
    "output_types": {"stream": 1},
    "stream_names": ["stdout"],
    "mime_types": [],
    "has_image": false,
    "image_count": 0,
    "has_error": false
  },
  "execution_count": 1
}
```

### MULTI 다음 Operation 대기

```json
{
  "schema_version": "2.0",
  "execution_id": "5fa09f84-6b86-4fd2-9ed4-4438def57e28",
  "status": "WAITING_FOR_OPERATION",
  "operation_id": "3cfbafec-a1cb-4fa0-a8ad-8e61bd43514d",
  "operation_wait_expires_at": "2026-08-26T05:15:30.123456+00:00",
  "version": 4
}
```

### Execution 성공

```json
{
  "schema_version": "2.0",
  "execution_id": "5fa09f84-6b86-4fd2-9ed4-4438def57e28",
  "status": "SUCCEEDED",
  "failure_type": null,
  "retry_strategy": "NOT_RETRYABLE",
  "retry_from_sequence": null,
  "runtime_session_cleanup_status": "SUCCEEDED",
  "result_available": true,
  "result_ref": {
    "scope": "EXECUTION",
    "operation_id": null,
    "step_id": null
  }
}
```

## 9. SINGLE 처리 흐름

정상 성공 시 대표 순서는 다음과 같다.

```text
execution.submitted
→ execution.started
→ execution.step_started
→ execution.step_succeeded
→ ... 다음 Step 반복
→ execution.operation_succeeded
→ execution.succeeded
```

Agent 권장 처리:

1. `POST /api/v1/executions` 응답에서 `execution_id`를 저장한다.
2. `executor.events`에서 같은 `execution_id`의 이벤트를 기다린다.
3. Step 이벤트는 진행상황 표시와 결과 manifest 조기 참조에 사용할 수 있다.
4. `execution.succeeded`, `execution.failed`, `execution.cancelled` 중 하나를 받으면
   terminal로 판단한다.
5. 성공/실패이면 `GET /api/v1/executions/{execution_id}/result`로 원본 상태와 Step 결과
   참조를 다시 조회한다.
6. 취소이면 Result가 생성되지 않으므로 Execution 상세 상태만 확인한다.

## 10. MULTI 처리 흐름

첫 Operation 성공 후 대표 순서는 다음과 같다.

```text
execution.submitted
→ execution.started
→ Step 이벤트들
→ execution.operation_succeeded
→ execution.waiting_for_operation
```

Agent 권장 처리:

1. `execution.waiting_for_operation`을 현재 Operation 완료 wake-up으로 사용한다.
2. Operation 결과 API를 조회한다.
3. 이벤트의 `version`과 최신 조회 상태를 확인한다.
4. 후속 코드가 필요하면 다음 API를 호출한다.

```http
POST /api/v1/executions/{execution_id}/operations
```

5. 더 실행할 코드가 없으면 다음 API를 호출한다.

```http
POST /api/v1/executions/{execution_id}/finalize
```

후속 Operation은 다음과 같이 진행된다.

```text
execution.operation_submitted
→ execution.resumed
→ Step 이벤트들
→ execution.operation_succeeded 또는 execution.operation_failed
→ execution.waiting_for_operation
```

최종화 후에는 `execution.succeeded` 또는 `execution.failed` terminal 이벤트를 기다리고
Execution Result API를 조회한다.

## 11. Consumer 구현 규칙

### 11.1 Consumer group

- Agent 서비스 전용의 안정적인 consumer group 이름을 사용한다.
- 같은 Agent 서비스의 여러 replica는 group을 공유하고 consumer name은 각각 다르게 한다.
- 완전히 다른 소비 목적의 서비스는 별도 group을 사용한다.
- 신규 이벤트부터 받을 때 group 시작 ID는 `$`를 사용한다.
- 기존 이벤트 전체를 재생해야 할 때만 `0-0`을 사용한다.

### 11.2 중복 제거와 ACK

1. Stream entry를 읽는다.
2. envelope와 payload를 검증한다.
3. `event_id` 중복 여부를 Agent DB에서 확인한다.
4. 중복 제거 레코드와 Agent 상태 변경을 같은 DB 트랜잭션으로 저장한다.
5. 트랜잭션 commit 후 Redis Stream message ID를 `XACK`한다.

Operation ID나 Execution ID로 중복 제거하면 안 된다. 하나의 Execution과 Operation에서도
여러 이벤트가 발생하며, 재시도는 같은 논리 Operation을 다시 사용할 수 있다.

### 11.3 Pending 복구

Consumer가 처리 도중 죽으면 메시지가 Pending에 남는다. 다른 replica가 일정 idle 시간
뒤 `XAUTOCLAIM`으로 가져와 다시 처리해야 한다. at-least-once이므로 재처리 중복은 정상
상황이며 `event_id` 멱등성으로 흡수해야 한다.

### 11.4 잘못된 이벤트

파싱 또는 계약 검증에 실패한 이벤트를 정상 처리한 것처럼 버리면 안 된다. Agent가
정한 재시도 횟수 이후 Agent 소유 DLQ로 이동하고 운영자가 확인할 수 있게 해야 한다.
`executor.events.dlq`는 이 정책을 위한 예약 이름이며 Executor가 대신 관리하지 않는다.

### 11.5 순서와 상태 재조정

Redis 도착 순서만으로 상태 머신을 확정하지 않는다. 중복, 지연, 재정렬, Agent 재시작이
가능하므로 다음 원칙을 사용한다.

- `event_id`로 중복 제거한다.
- `execution_id`, `operation_id`, `execution_attempt_id`, `step_id`로 대상을 상관관계화한다.
- MULTI 명령에는 최신 `version`을 사용한다.
- Operation/terminal 경계에서는 Executor Result/Detail API를 다시 조회한다.
- 이벤트와 API 상태가 다르면 PostgreSQL 기반 API 응답을 원본으로 사용한다.

## 12. Agent가 주로 반응할 이벤트

| 이벤트 | Agent 처리 |
|---|---|
| `execution.submitted` | ID 매핑 저장, 대기 상태 표시 |
| `execution.step_started` | 진행 중 Step 표시 |
| `execution.step_succeeded` | 출력 요약 저장, 필요하면 공유 PV manifest 읽기 |
| `execution.step_failed` | 실패 요약 저장, manifest에서 오류 원문 확인 가능 |
| `execution.waiting_for_operation` | MULTI 결과 조회 후 후속 Operation 또는 finalize 결정 |
| `execution.succeeded` | Execution Result 조회 후 최종 리포트/답변 작성 |
| `execution.failed` | Result 조회 후 실패 원인과 보존 결과 전달, retry 가능성 판단 |
| `execution.cancelled` | 취소 완료 알림, 실행 잠금 해제 |
| `execution.retry_deferred` | Runtime 복구 대기 상태 표시 |
| Runtime abort/cleanup 이벤트 | 운영 상태 표시 및 장애 진단에 사용 |

## 13. 관련 문서와 예제

- [Execution Result API](get-execution-result.md)
- [Step Result Manifest 1.0](step-result-manifest.md)
- `docs/execution-events-v2.md`
- `scripts/agent_event_consumer_example.py`

`scripts/agent_event_consumer_example.py`는 consumer group 생성, `XAUTOCLAIM`, envelope 검증,
`event_id` 기반 중복 제거, 상태 저장 후 ACK 순서를 보여준다. 예제는 SQLite를 사용하지만
운영 Agent는 자신의 PostgreSQL 같은 영속 DB에서 중복 제거와 상태 변경을 하나의
트랜잭션으로 처리해야 한다.
