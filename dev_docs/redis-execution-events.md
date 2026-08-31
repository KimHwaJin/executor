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

Execution 상태 변경, 영구 ExecutionEvent, 전송용 OutboxEvent 생성은 하나의 PostgreSQL
트랜잭션으로 처리한다.
Outbox Publisher가 커밋된 이벤트를 Event Stream에 발행한다.

- 같은 `event_id`가 두 번 이상 전달될 수 있다.
- 소비자는 `event_id`를 영속적으로 저장해 중복 처리하지 않는다.
- `event_sequence`는 Execution마다 `1`부터 증가하며 DB 트랜잭션에서 발급한다.
- Redis Stream message ID와 `event_id`는 서로 다른 값이다.
- Publisher는 같은 Execution의 앞 순번이 발행될 때까지 뒤 순번을 보류한다.
- 장애 복구와 소비자 병렬 처리까지 고려해 소비자는 순번을 검증한다.
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

### 출력 제한 실패와 부분 결과

Jupyter가 IOPub 데이터/메시지 전송량 제한으로 출력을 차단한 경우에도 기존
`OUTPUT_LIMIT_EXCEEDED` 실패 체계를 사용한다. 이벤트 스키마 변경은 없다.

- Step / Operation 완료 이벤트의 `status`는 `FAILED`이며, `error.message`에
  출력 제한 사유가 남는다. 일반 사용자 코드가 같은 경고 문구를 출력하는 것은
  서버의 제한 신호로 취급하지 않는다.
- SINGLE은 `execution.completed`의 `status=FAILED`,
  `error.code=EXECUTION_OUTPUT_LIMIT_EXCEEDED`로 종료한다.
- MULTI는 런타임 유휴 상태 확인에 성공하면 Execution이
  `WAITING_FOR_OPERATION`이 될 수 있다. 이는 해당 Operation의 성공을 뜻하지 않는다.
- 기존 계약상 불완전한 `result_ref`는 이벤트에 포함하지 않는다. 부분 증거가
  필요하면 Step 상세 또는 위 Result API에서 참조를 조회한다.
  참조의 `complete=false`와 실패 사유를 확인하고, 보존된 경고/출력을 전체 결과로
  해석하지 않는다. 이미 서버가 버린 출력을 복원하는 기능은 아니다.

검증 범위와 운영상 주의점은
[Runtime output completeness](../docs/runtime-output-completeness.md)를 참고한다.

## 4. 공통 이벤트 Envelope

모든 외부 실행 이벤트는 다음 일곱 필드를 최상위에 가진다.

```json
{
  "event_id": "evt-01K...",
  "event_type": "execution.step_completed",
  "schema_version": "1.0",
  "execution_id": "exec-01K...",
  "event_sequence": 4,
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
| `event_sequence` | integer | O | Execution별 논리 이벤트 순번, `1`부터 증가 |
| `payload` | object | O | 이벤트별 데이터 |
| `occurred_at` | RFC 3339 UTC datetime | O | 이벤트에 해당하는 상태 전환 시각 |

`payload`에는 `execution_id`와 `schema_version`을 반복하지 않는다. Redis Stream
entry의 값은 문자열이므로 wire level에서는 `payload`가 JSON 문자열이다. 소비자는
이를 JSON object로 파싱한다.

`event_sequence`는 Redis Stream message ID나 Step의 `sequence`와 다르다. Redis 발행
시점이 아니라 영구 이벤트와 Outbox를 저장하는 PostgreSQL 트랜잭션에서 확정되며,
`execution_id + event_sequence` 조합은 유일하다.

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
  "event_sequence": 1,
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
  "event_sequence": 2,
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
  "event_sequence": 3,
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
  "event_sequence": 4,
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
  "event_sequence": 5,
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
  "execution_status": "WAITING_FOR_OPERATION",
  "continuation": {
    "allowed": true,
    "expected_version": 4,
    "expires_at": "2026-08-26T10:25:40.123Z"
  },
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

`step_results`는 sequence 순으로 정렬하며, 저장이 완료되어 참조할 수 있는 Step 결과만
포함한다. 따라서 결과 파일이 없는 취소 Step이나 비정상 복구 상태에서는
`step_summary.completed`보다 항목 수가 적을 수 있다. Retry된 Step은 과거 Attempt를 모두
나열하지 않고 Operation 종료 시점의 최종 Attempt만 포함한다. 전체 Attempt 이력은
Result API로 확인한다.

`continuation`은 다음 조건을 모두 만족할 때만 객체로 제공한다.

- lifecycle이 MULTI임
- Execution이 `WAITING_FOR_OPERATION` 상태임
- 다음 Operation 접수 가능 시간이 지나지 않음

따라서 Operation이 실패해도 Runtime Session을 안전하게 유지했고 후속 보정 Operation을
받을 수 있다면 `continuation`을 제공한다. Runtime Session을 잃었거나 Execution 자체가
종료됐다면 `null`이다.

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
  "event_sequence": 6,
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
  "event_sequence": 6,
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
      "expires_at": "2026-08-27T10:20:00.123Z"
    },
    "error": {
      "code": "EXECUTION_TOOL_ERROR",
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

`operation_summary.total`에는 등록된 모든 Operation이 포함된다. 인프라 장애 복구처럼
Execution이 Operation 상태를 종결하기 전에 종료된 예외 상황에서는 성공, 실패, 취소의
합이 `total`보다 작을 수 있다.

`retry`는 기술적 재시도 가능 여부와 Executor의 Retry Window 정책을 모두 만족할 때만
객체로 제공한다.

- `from_step_id`: 재시작할 실패 Step ID
- `expires_at`: Retry 요청 만료 시각

Retry API는 `expected_version`을 받지 않는다. `idempotency_key`와 Executor가 보관한
실패·Runtime 상태를 기준으로 재시도를 접수한다.

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

Operation이 실패했더라도 `execution.operation_completed`의 `continuation.allowed`가
`true`이면 Agent는 실패 결과를 읽고 보정 Operation을 추가할 수 있다. 이 경계에서는
`execution.completed`를 발행하지 않는다.

### 13.3 SINGLE Step 실패 후 Retry

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

### 14.1 중복·순서·누락 처리

소비자는 Execution별 `last_event_sequence`를 Agent 소유 DB 또는 LangGraph checkpoint에
저장한다. 이 처리는 LLM 프롬프트나 그래프 업무 노드가 아니라 공통 Event Subscriber가
담당한다.

- `event_sequence == last + 1`: 정상 처리하고 checkpoint를 갱신
- `event_sequence <= last`: 중복 또는 늦은 이벤트이므로 적용하지 않고 ACK
- `event_sequence > last + 1`: 현재 이벤트를 먼저 적용하지 않고 누락 구간 복구

누락 구간은 다음 REST 또는 MCP 조회로 가져온다.

```http
GET /api/v1/executions/{execution_id}/events?after_sequence={last}&limit=500
```

```text
execution_event_list(execution_id, after_sequence=last, limit=500)
```

페이지의 이벤트를 `event_sequence` 순서로 적용한 뒤 원래 Redis 이벤트로 돌아간다.
정상적으로 연속된 Redis 이벤트에는 이 API를 호출하지 않는다.

### 14.2 실행 중 결과 사용

소비자는 `execution.step_completed`의 `result_ref`를 사용해 공유 PV의 Step Manifest와
출력 파일을 읽는다. MULTI Agent는 이 결과로 후속 계획을 생성할 수 있다.

### 14.3 Operation 경계

`execution.operation_completed`는 해당 Operation에서 실제 수행된 Step의 최신 결과
참조를 `step_results`로 다시 제공한다. 개별 Step 이벤트를 모두 처리했다면 추가 API
호출은 필요 없다.

### 14.4 Execution 경계

최종 리포트 작성 전 전체 정합성을 확인하려면 Result API를 한 번 호출한다.

```http
GET /api/v1/executions/{execution_id}/result
```

모든 Step 이벤트를 중복 제거하여 영속적으로 처리했고 누락이 없음을 보장할 수 있다면
누적한 `result_ref`를 그대로 사용할 수도 있다.

### 14.5 정합성 재조회 조건

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
- Execution별 마지막 연속 `event_sequence`를 영속적으로 저장한다.
- 순번이 건너뛰면 Event Subscriber가 이벤트 이력 API로 누락 구간만 복구한다.
- Redis 처리와 복구 로직을 LLM 또는 LangGraph 업무 노드에 직접 구현하지 않는다.
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
