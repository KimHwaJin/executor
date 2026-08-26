# Executor Redis 실행 이벤트 계약

## 1. 문서 상태와 목적

이 문서는 Executor가 외부 시스템에 발행하는 Redis 실행 이벤트의 목표 계약을
정의한다. Agent를 포함한 모든 외부 소비자는 이 문서를 기준으로 이벤트를 구현한다.

- 이벤트 스키마 버전: `1.0`
- 대상 Stream 기본 이름: `executor.events`
- 전달 방식: Redis Streams
- 전달 보장: at-least-once
- 상태 원본: PostgreSQL
- 결과 원본: 공유 PV의 Step 결과 파일

이벤트는 상태 원본이 아니다. 이벤트는 상태 변경을 알리고, 실행 중 생성된 Step 결과
파일을 찾을 수 있게 하는 통합 계약이다. 상태 충돌, 이벤트 누락 또는 재처리 시에는
Executor API와 PostgreSQL 상태를 기준으로 조회/복구한다.

## 2. Stream과 전달 원칙

Executor는 내부 명령과 외부 이벤트를 서로 다른 Stream으로 관리한다.

| Stream | 기본 이름 | 생산자 | 소비자 | 용도 |
|---|---|---|---|---|
| Work Stream | `executor.work` | Executor API | Executor Worker | 내부 실행 명령 전달 |
| Event Stream | `executor.events` | Outbox Publisher | Agent 등 외부 시스템 | 실행 상태와 결과 알림 |

외부 시스템은 `executor.events`만 소비한다. 독립적으로 모든 이벤트를 받아야 하는
시스템끼리는 consumer group을 공유하지 않는다.

Execution 상태 변경과 OutboxEvent 생성은 하나의 PostgreSQL 트랜잭션으로 처리한다.
Outbox Publisher가 커밋된 이벤트를 Event Stream에 발행한다.

- 같은 `event_id`가 두 번 이상 전달될 수 있다.
- 소비자는 `event_id`를 영속적으로 저장해 중복 처리하지 않는다.
- Redis Stream message ID와 `event_id`는 서로 다른 값이다.
- 재전달 과정에서 애플리케이션 처리 순서가 달라질 수 있다.
- 충돌하거나 누락된 이벤트는 Result API로 정합성을 확인한다.

## 3. 결과 저장 원칙

공유 PV에는 Step 결과만 물리 파일로 저장한다.

```text
executions/{execution_id}/
└─ operations/{operation_id}/
   └─ steps/{step_id}/
      └─ attempts/{attempt_id}/
         ├─ manifest.json
         └─ outputs/
            ├─ 000000-stream-00.txt
            └─ 000001-display_data-00.png
```

디렉터리 모양은 Executor 내부 구현이며 외부 계약이 아니다. 소비자는 이벤트나 API가
반환한 `relative_path`를 그대로 사용하고 경로를 직접 조립하지 않는다.

다음 집계 파일은 생성하지 않는다.

- `operation-result.json`
- `execution-result.json`

Operation과 Execution의 전체 결과 인덱스는 PostgreSQL을 기준으로 Result API가
조립한다.

```http
GET /api/v1/executions/{execution_id}/result
```

## 4. 공통 이벤트 Envelope

모든 외부 실행 이벤트는 다음 여섯 필드만 최상위에 가진다.

```json
{
  "event_id": "evt-01K...",
  "event_type": "execution.step_completed",
  "schema_version": "1.0",
  "execution_id": "exec-01K...",
  "payload": {},
  "occurred_at": "2026-08-26T10:15:40.123Z"
}
```

| 필드 | 타입 | 필수 | 의미 |
|---|---|---:|---|
| `event_id` | string | O | 이벤트 멱등 처리용 고유 ID |
| `event_type` | enum | O | 이벤트 종류 |
| `schema_version` | literal `1.0` | O | 외부 이벤트 계약 버전 |
| `execution_id` | string | O | 전체 실행의 고정 식별자 |
| `payload` | object | O | 이벤트별 데이터 |
| `occurred_at` | RFC 3339 UTC datetime | O | 이벤트에 해당하는 상태 전환 시각 |

`payload`에는 `execution_id`와 `schema_version`을 반복하지 않는다. Redis Stream
entry의 값은 문자열이므로 wire level에서는 `payload`가 JSON 문자열이다. 소비자는
이를 JSON object로 파싱한다.

## 5. 공통 하위 객체

### 5.1 Runtime

```json
{
  "provider": "JUPYTER",
  "profile": "basic",
  "target_id": "runtime-target-01K...",
  "session_id": "runtime-session-01K..."
}
```

| 필드 | 타입 | 필수 | 의미 |
|---|---|---:|---|
| `provider` | string | O | Runtime Provider 종류 |
| `profile` | string | O | 요청에 사용된 Runtime Profile |
| `target_id` | string | O | 실제 할당된 Runtime Target ID |
| `session_id` | string 또는 null | O | Runtime Session ID |

Session 개념이 없는 Provider는 `session_id`를 `null`로 제공한다. endpoint, token,
credential 등 비밀정보는 포함하지 않는다.

### 5.2 Operation, Step, Attempt

Operation 시작 이벤트의 Operation 객체:

```json
{
  "id": "op-01K...",
  "number": 1,
  "step_count": 2
}
```

- `id`: Executor가 발급한 Operation ID
- `number`: Execution 안에서 생성된 순서이며 `1`부터 시작
- `step_count`: 등록된 전체 Step 수

Step 객체:

```json
{
  "id": "step-01K...",
  "sequence": 0
}
```

- `id`: Executor가 발급한 논리 Step ID
- `sequence`: Operation 내부 실행 순서이며 `0`부터 시작
- Retry해도 `id`와 `sequence`는 변경하지 않음

Attempt 객체:

```json
{
  "id": "attempt-01K...",
  "number": 1,
  "reason": "INITIAL"
}
```

- `id`: 해당 실행 시도 ID
- `number`: Execution 실행 시도 순번이며 `1`부터 시작
- `reason`: `INITIAL` 또는 `RETRY`

Attempt는 PostgreSQL에서 Execution의 실행 시도 이력으로 관리한다. 외부 이벤트에는
실제 수행 시도를 구분해야 하는 Step 이벤트와 Step 결과 항목에만 포함한다.

### 5.3 Result reference

```json
{
  "storage": "SHARED_PV",
  "relative_path": "executions/.../manifest.json",
  "media_type": "application/json",
  "size_bytes": 1842,
  "checksum_sha256": "f7b6..."
}
```

| 필드 | 타입 | 의미 |
|---|---|---|
| `storage` | literal `SHARED_PV` | Agent와 Executor가 공유하는 스토리지 |
| `relative_path` | string | 공유 PV 공통 루트를 기준으로 한 상대경로 |
| `media_type` | string | 참조 파일의 MIME type |
| `size_bytes` | integer, 0 이상 | 참조 파일 크기 |
| `checksum_sha256` | string | 파일 무결성 확인용 SHA-256 |

`relative_path`는 반드시 상대경로여야 한다. `..`, 절대경로 및 공유 PV 루트 이탈은
허용하지 않는다.

### 5.4 Output summary와 Error summary

```json
{
  "count": 2,
  "content_types": [
    "image/png",
    "text/plain"
  ]
}
```

- `count`: Runtime이 생성한 출력 record 수
- `content_types`: 출력 representation의 중복 없는 MIME type 목록
- `content_types`는 오름차순으로 정렬
- 전체 텍스트, 이미지 Base64 및 traceback은 포함하지 않음

```json
{
  "code": "STEP_EXECUTION_FAILED",
  "message": "Input data does not contain the required column.",
  "retryable": true
}
```

- `code`: 소비자가 분기 처리할 수 있는 안정적인 오류 코드
- `message`: 사용자에게 전달 가능한 제한된 오류 요약
- `retryable`: 오류 자체가 기술적으로 재시도 가능한지 표시

내부 stack trace와 비밀값은 Error summary에 포함하지 않는다.

## 6. 이벤트 종류

| 이벤트 | 발행 횟수 | 의미 |
|---|---:|---|
| `execution.started` | Execution당 최초 한 번 | Runtime 준비 후 최초 실행 시작 |
| `execution.operation_started` | Operation당 최초 한 번 | Operation 실행 시작 |
| `execution.step_started` | Step 수행 시도마다 | Step 실행 시도 시작 |
| `execution.step_completed` | Step 수행 시도마다 | Step 실행 시도 종료 |
| `execution.operation_completed` | Operation 수행 주기마다 | Operation 종료 또는 재시도 결과 갱신 |
| `execution.completed` | Execution 수행 주기마다 | Execution 성공, 실패 또는 취소 |

## 7. `execution.started`

Execution이 최초 Runtime 준비를 마치고 실행 가능한 상태가 됐을 때 한 번 발행한다.
Retry에서는 다시 발행하지 않는다. Runtime 준비 전에 실패하면 이 이벤트 없이
`execution.completed`가 발생할 수 있다.

```json
{
  "event_id": "evt-01K...",
  "event_type": "execution.started",
  "schema_version": "1.0",
  "execution_id": "exec-01K...",
  "payload": {
    "status": "RUNNING",
    "runtime": {
      "provider": "JUPYTER",
      "profile": "basic",
      "target_id": "runtime-target-01K...",
      "session_id": "runtime-session-01K..."
    }
  },
  "occurred_at": "2026-08-26T10:15:32.456Z"
}
```

| payload 필드 | 타입 | 필수 | 규칙 |
|---|---|---:|---|
| `status` | literal `RUNNING` | O | Execution 변경 후 상태 |
| `runtime` | Runtime | O | 최초 할당된 실제 Runtime 정보 |

## 8. `execution.operation_started`

Operation이 최초로 `RUNNING` 상태가 되고 첫 Step 실행을 시작하기 직전에 한 번
발행한다. MULTI에서 새 Operation이 추가되면 새로 발행한다. 같은 Operation의 Step
Retry에서는 다시 발행하지 않는다.

```json
{
  "event_id": "evt-01K...",
  "event_type": "execution.operation_started",
  "schema_version": "1.0",
  "execution_id": "exec-01K...",
  "payload": {
    "status": "RUNNING",
    "operation": {
      "id": "op-01K...",
      "number": 1,
      "step_count": 2
    }
  },
  "occurred_at": "2026-08-26T10:15:33.012Z"
}
```

| payload 필드 | 타입 | 필수 | 규칙 |
|---|---|---:|---|
| `status` | literal `RUNNING` | O | Operation 변경 후 상태 |
| `operation` | Operation | O | 시작된 Operation |

## 9. `execution.step_started`

개별 Step의 실제 실행 시도가 시작될 때 발행한다. Retry에서는 같은 Operation과 Step
식별자를 유지하고 새로운 Attempt를 제공한다.

```json
{
  "event_id": "evt-01K...",
  "event_type": "execution.step_started",
  "schema_version": "1.0",
  "execution_id": "exec-01K...",
  "payload": {
    "status": "RUNNING",
    "operation": {
      "id": "op-01K...",
      "number": 1
    },
    "step": {
      "id": "step-01K...",
      "sequence": 0
    },
    "attempt": {
      "id": "attempt-01K...",
      "number": 1,
      "reason": "INITIAL"
    }
  },
  "occurred_at": "2026-08-26T10:15:34.123Z"
}
```

| payload 필드 | 타입 | 필수 | 규칙 |
|---|---|---:|---|
| `status` | literal `RUNNING` | O | Step 변경 후 상태 |
| `operation` | object | O | Operation ID와 순번 |
| `step` | Step | O | 실행을 시작한 Step |
| `attempt` | Attempt | O | 이번 Step이 속한 수행 시도 |

## 10. `execution.step_completed`

Step이 `SUCCEEDED`, `FAILED` 또는 `CANCELLED`로 종료된 뒤 발행한다. 결과가 있으면
Manifest를 원자적으로 저장하고 검증한 다음 DB 상태와 OutboxEvent를 커밋한다.

```text
Step 실행 종료
→ 출력 파일 저장
→ manifest.json 원자적 저장
→ DB Step 상태와 result_ref 저장
→ OutboxEvent 저장
→ execution.step_completed 발행
```

성공 예시:

```json
{
  "event_id": "evt-01K...",
  "event_type": "execution.step_completed",
  "schema_version": "1.0",
  "execution_id": "exec-01K...",
  "payload": {
    "status": "SUCCEEDED",
    "operation": {
      "id": "op-01K...",
      "number": 1
    },
    "step": {
      "id": "step-01K...",
      "sequence": 0
    },
    "attempt": {
      "id": "attempt-01K...",
      "number": 1,
      "reason": "INITIAL"
    },
    "result_ref": {
      "storage": "SHARED_PV",
      "relative_path": "executions/.../manifest.json",
      "media_type": "application/json",
      "size_bytes": 1842,
      "checksum_sha256": "f7b6..."
    },
    "output_summary": {
      "count": 2,
      "content_types": [
        "image/png",
        "text/plain"
      ]
    },
    "error": null
  },
  "occurred_at": "2026-08-26T10:15:40.123Z"
}
```

실패 시 `status`와 `error`는 다음처럼 변경된다.

```json
{
  "status": "FAILED",
  "error": {
    "code": "STEP_EXECUTION_FAILED",
    "message": "Input data does not contain the required column.",
    "retryable": true
  }
}
```

| payload 필드 | 타입 | 필수 | 규칙 |
|---|---|---:|---|
| `status` | enum | O | `SUCCEEDED`, `FAILED`, `CANCELLED` |
| `operation` | object | O | Operation ID와 순번 |
| `step` | Step | O | 종료된 Step |
| `attempt` | Attempt | O | 이번 수행 시도 |
| `result_ref` | ResultReference 또는 null | O | 완성된 Step Manifest 참조 |
| `output_summary` | OutputSummary 또는 null | O | 저장된 출력 요약 |
| `error` | ErrorSummary 또는 null | O | 실패 또는 취소 요약 |

- `SUCCEEDED`이면 `result_ref`와 `output_summary`가 반드시 존재한다.
- `FAILED`라도 실행 오류와 부분 출력이 저장됐다면 참조를 제공한다.
- 결과 저장 자체가 실패하면 `result_ref`와 `output_summary`는 `null`이다.
- `CANCELLED`는 완전한 결과가 보존된 경우에만 참조를 제공한다.
- 성공이면 `error`는 `null`이다.

## 11. `execution.operation_completed`

Operation이 `SUCCEEDED`, `FAILED` 또는 `CANCELLED`로 종료될 때 발행한다. 별도 결과
파일을 만들지 않고 실제 수행된 Step의 최종 결과 참조를 `step_results`에 포함한다.
Operation당 최대 Step 수는 Executor 설정으로 제한한다.

MULTI 성공 예시:

```json
{
  "event_id": "evt-01K...",
  "event_type": "execution.operation_completed",
  "schema_version": "1.0",
  "execution_id": "exec-01K...",
  "payload": {
    "status": "SUCCEEDED",
    "execution_status": "WAITING_FOR_OPERATION",
    "operation": {
      "id": "op-01K...",
      "number": 1
    },
    "step_summary": {
      "total": 2,
      "completed": 2,
      "succeeded": 2,
      "failed": 0,
      "cancelled": 0
    },
    "step_results": [
      {
        "step_id": "step-01K...",
        "sequence": 0,
        "status": "SUCCEEDED",
        "attempt": {
          "id": "attempt-01K...",
          "number": 1,
          "reason": "INITIAL"
        },
        "result_ref": {
          "storage": "SHARED_PV",
          "relative_path": "executions/.../manifest.json",
          "media_type": "application/json",
          "size_bytes": 1842,
          "checksum_sha256": "f7b6..."
        }
      }
    ],
    "continuation": {
      "allowed": true,
      "expected_version": 3,
      "expires_at": "2026-08-26T10:25:40.123Z"
    },
    "error": null
  },
  "occurred_at": "2026-08-26T10:15:40.456Z"
}
```

실패 시 주요 필드는 다음처럼 변경된다.

```json
{
  "status": "FAILED",
  "execution_status": "FAILED",
  "continuation": null,
  "error": {
    "code": "OPERATION_STEP_FAILED",
    "message": "Operation stopped because a step failed.",
    "step_id": "step-02K...",
    "retryable": true
  }
}
```

| payload 필드 | 타입 | 필수 | 규칙 |
|---|---|---:|---|
| `status` | enum | O | `SUCCEEDED`, `FAILED`, `CANCELLED` |
| `execution_status` | string | O | Operation 종료 후 Execution 상태 |
| `operation` | object | O | Operation ID와 순번 |
| `step_summary` | object | O | 등록 및 완료 Step 상태 집계 |
| `step_results` | array | O | 실제 수행된 Step의 최종 결과 참조 |
| `continuation` | object 또는 null | O | 다음 Operation 추가 정보 |
| `error` | object 또는 null | O | 실패 또는 취소 요약 |

`step_summary.completed`는 성공, 실패, 취소 Step의 합이다. 실행되지 않은 Step 수는
`total - completed`로 계산한다.

`step_results`는 sequence 순으로 정렬한다. Retry된 Step은 과거 Attempt를 모두
나열하지 않고 Operation 종료 시점의 최종 Attempt만 포함한다. 전체 Attempt 이력은
Result API로 확인한다.

`continuation`은 다음 조건을 모두 만족할 때만 객체로 제공한다.

- lifecycle이 MULTI임
- Operation이 성공함
- 다음 Operation 접수 가능 시간이 지나지 않음

그 외에는 `null`이다. `expected_version`은 다음 Operation 요청의 낙관적 잠금 값이며,
`expires_at`은 접수 만료 시각이다.

## 12. `execution.completed`

Execution의 현재 수행 주기가 `SUCCEEDED`, `FAILED` 또는 `CANCELLED`로 종료될 때
발행한다. 별도 Execution 결과 파일과 전체 Step 결과 목록은 포함하지 않는다.

성공 예시:

```json
{
  "event_id": "evt-01K...",
  "event_type": "execution.completed",
  "schema_version": "1.0",
  "execution_id": "exec-01K...",
  "payload": {
    "status": "SUCCEEDED",
    "operation_summary": {
      "total": 2,
      "succeeded": 2,
      "failed": 0,
      "cancelled": 0
    },
    "retry": null,
    "error": null
  },
  "occurred_at": "2026-08-26T10:20:00.123Z"
}
```

실패 예시:

```json
{
  "event_id": "evt-01K...",
  "event_type": "execution.completed",
  "schema_version": "1.0",
  "execution_id": "exec-01K...",
  "payload": {
    "status": "FAILED",
    "operation_summary": {
      "total": 2,
      "succeeded": 1,
      "failed": 1,
      "cancelled": 0
    },
    "retry": {
      "allowed": true,
      "from_step_id": "step-02K...",
      "expected_version": 7,
      "expires_at": "2026-08-27T10:20:00.123Z"
    },
    "error": {
      "code": "EXECUTION_STEP_FAILED",
      "message": "Execution stopped because a step failed.",
      "operation_id": "op-02K...",
      "step_id": "step-02K...",
      "retryable": true
    }
  },
  "occurred_at": "2026-08-26T10:20:00.123Z"
}
```

취소 시 `status`는 `CANCELLED`, `retry`는 `null`이며 오류 코드는
`EXECUTION_CANCELLED`를 사용한다.

| payload 필드 | 타입 | 필수 | 규칙 |
|---|---|---:|---|
| `status` | enum | O | `SUCCEEDED`, `FAILED`, `CANCELLED` |
| `operation_summary` | object | O | Operation 최종 상태 집계 |
| `retry` | object 또는 null | O | 재시도 접수 정보 |
| `error` | object 또는 null | O | 실패 또는 취소 요약 |

`retry`는 기술적 재시도 가능 여부와 Executor의 Retry Window 정책을 모두 만족할 때만
객체로 제공한다.

- `from_step_id`: 재시작할 실패 Step ID
- `expected_version`: Retry 요청에 사용할 낙관적 잠금 버전
- `expires_at`: Retry 요청 만료 시각

실패 후 Retry가 수행되면 새로운 Step Attempt 이벤트부터 다시 시작하고, 다시 종료될
때 같은 `execution_id`로 새로운 `execution.completed`를 발행한다.

## 13. 이벤트 흐름

### 13.1 SINGLE 성공

```text
execution.started
→ execution.operation_started
→ execution.step_started
→ execution.step_completed
→ execution.operation_completed
→ execution.completed
```

### 13.2 MULTI

```text
execution.started
→ execution.operation_started         # Operation 1
→ execution.step_started
→ execution.step_completed
→ execution.operation_completed       # WAITING_FOR_OPERATION

POST /api/v1/executions/{execution_id}/operations

→ execution.operation_started         # Operation 2
→ execution.step_started
→ execution.step_completed
→ execution.operation_completed       # WAITING_FOR_OPERATION

POST /api/v1/executions/{execution_id}/finalize

→ execution.completed
```

### 13.3 Step 실패 후 Retry

```text
execution.started
→ execution.operation_started
→ execution.step_started              # Step A, Attempt 1
→ execution.step_completed            # Step A, SUCCEEDED
→ execution.step_started              # Step B, Attempt 1
→ execution.step_completed            # Step B, FAILED
→ execution.operation_completed       # FAILED
→ execution.completed                 # FAILED, retry.allowed=true

POST /api/v1/executions/{execution_id}/retry

→ execution.step_started              # Step B, Attempt 2
→ execution.step_completed            # Step B, SUCCEEDED
→ execution.step_started              # Step C, Attempt 2
→ execution.step_completed            # Step C, SUCCEEDED
→ execution.operation_completed       # SUCCEEDED
→ execution.completed                 # SUCCEEDED
```

Retry에서는 `execution.started`와 `execution.operation_started`를 다시 발행하지 않는다.

## 14. 소비자 처리 규칙

### 14.1 실행 중 결과 사용

소비자는 `execution.step_completed`의 `result_ref`를 사용해 공유 PV의 Step Manifest와
출력 파일을 읽는다. MULTI Agent는 이 결과로 후속 계획을 생성할 수 있다.

### 14.2 Operation 경계

`execution.operation_completed`는 해당 Operation에서 실제 수행된 Step의 최신 결과
참조를 `step_results`로 다시 제공한다. 개별 Step 이벤트를 모두 처리했다면 추가 API
호출은 필요 없다.

### 14.3 Execution 경계

최종 리포트 작성 전 전체 정합성을 확인하려면 Result API를 한 번 호출한다.

```http
GET /api/v1/executions/{execution_id}/result
```

모든 Step 이벤트를 중복 제거하여 영속적으로 처리했고 누락이 없음을 보장할 수 있다면
누적한 `result_ref`를 그대로 사용할 수도 있다.

### 14.4 정합성 재조회 조건

다음 상황에서는 이벤트 payload만으로 상태를 확정하지 않는다.

- 처음 보는 `operation.number`가 예상 순서와 다름
- 같은 Step에서 더 낮은 `attempt.number`가 늦게 도착함
- Operation 집계와 처리한 Step 이벤트가 다름
- 결과 파일 크기 또는 체크섬이 일치하지 않음
- 동일 Execution에서 상충하는 종료 상태를 관찰함

이 경우 Result API 또는 세부 조회 API로 PostgreSQL 원본 상태를 확인한다.

## 15. 소비자 체크리스트

- `executor.events`에 전용 consumer group을 생성한다.
- 처리 완료 후에만 `XACK`한다.
- `event_id`를 DB에 저장하고 중복 이벤트를 무시한다.
- 지원하는 `schema_version`인지 먼저 검사한다.
- 알 수 없는 이벤트를 조용히 폐기하지 않고 별도 오류 채널에 기록한다.
- `execution_id`를 현재 상태 조회의 대표 키로 사용한다.
- Attempt ID를 Execution의 대표 조회 키로 사용하지 않는다.
- `relative_path`를 공유 PV 루트에 안전하게 결합한다.
- 결과 파일 크기와 SHA-256을 검증한다.
- 전체 텍스트와 이미지는 Redis가 아니라 결과 파일에서 읽는다.
- 최종 리포트 전에는 필요에 따라 Result API로 정합성을 확인한다.

## 16. 외부 이벤트 금지 정보

- Runtime endpoint, token, credential
- PostgreSQL 또는 Redis 연결정보
- 전체 코드 본문
- 전체 텍스트 출력
- 이미지 Base64
- 전체 traceback
- Worker ID, Lease ID, fencing token
- `operation-result.json` 또는 `execution-result.json` 참조

Worker, Lease 및 fencing 정보는 Executor 내부 제어와 운영 진단에만 사용한다.
