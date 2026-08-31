# Execution 진단 이력 조회

실행 실패뿐 아니라 출력 저장, 노트북 작성, 아티팩트 등록, 커널 정리 중 발생한
오류를 함께 확인하는 운영 조회 API다. 실행 결과 파일 조회를 대체하지 않는다.

```http
GET /api/v1/executions/{execution_id}/diagnostics
```

현재 REST로 제공한다. 공통 Pydantic 응답 모델은 분리돼 있지만 MCP Tool 추가는
후속 작업이다. 기존 실행 API, Redis 이벤트 envelope/payload, manifest 계약은
변경하지 않는다. 소비자가 모든 이벤트마다 이 API를 호출할 필요는 없다.

## 요청

| 항목 | 위치 | 필수/기본 | 의미 |
| --- | --- | --- | --- |
| `execution_id` | path | 필수 UUID | 조회할 실행 |
| `attempt_id` | query | 선택 UUID | 특정 실행 Attempt에 기록된 관측만 조회 |
| `operation_id` | query | 선택 UUID | 특정 Operation 관련 관측만 조회 |
| `step_id` | query | 선택 UUID | 특정 Step 관련 관측만 조회 |
| `cursor` | query | 선택 문자열 | 이전 응답의 `next_cursor`를 그대로 전달 |
| `limit` | query | 기본 100, 1–200 | 한 페이지 최대 건수 |

필터는 AND로 적용한다. 커서는 Execution과 필터 조합에 묶인다. 필터를 바꾸면
커서를 제거하고 첫 페이지부터 조회한다. 등록 순서 `created_at, id` 오름차순이다.
이는 상태 이벤트의 `event_sequence`와 무관하며, Redis 복구 커서가 아니다.

Execution 자체가 없으면 404, 잘못된 UUID/커서/limit이면 422다. 존재하는 실행에서
필터에 일치하는 관측이 없으면 빈 `items`를 반환한다. 항목별 추가 상세 API는 없다.

## 응답 필드

최상위는 `items`, `next_cursor`, `has_more`다. 아래는 `items[]`의 필드다.

| 필드 | 의미 |
| --- | --- |
| `id` | 진단 관측의 UUID. 실행/이벤트 ID가 아님 |
| `execution_id` | 소속 실행 |
| `attempt_id` | 기록한 Worker의 실행 Attempt. 취소 전용 소유권은 null |
| `operation_id` | 발생 시점의 Operation. 없는 경우 null |
| `step_id` | Step에 귀속 가능한 경우 해당 ID, 아니면 null |
| `step_sequence` | 위 Step의 실행 내 sequence. 없는 경우 null |
| `fencing_token` | 기록 당시 Worker 소유권 세대. 실행 이벤트 순번이 아님 |
| `occurred_at` | Executor가 오류를 관측한 시각. 원격 서버 내부 발생시각을 추측하지 않음 |
| `created_at`, `updated_at` | DB 기록 시각. 추가 전용 이력이므로 두 값이 같음 |
| `created_by_type`, `created_by` | 당시 실행의 actor 정보. Worker 자체 계정이라는 의미는 아님 |
| `updated_by_type`, `updated_by` | 생성 actor와 동일. actor가 없으면 모두 null |
| `diagnostic` | 아래의 공통 진단 정보 |

| `diagnostic` 필드 | 의미 |
| --- | --- |
| `code` | 기계적으로 분류할 수 있는 오류 코드 |
| `phase` | 오류를 관측한 처리 단계 |
| `category` | `EXECUTION`, `OUTPUT`, `NOTEBOOK`, `ARTIFACT`, `CLEANUP` |
| `origin` | `RUNTIME`, `RESULT_STORAGE`, `EXECUTOR`, `UNKNOWN`. 확실하지 않으면 UNKNOWN |
| `severity` | 현재 오류 관측만 저장하므로 `ERROR`. Execution 종료 상태와 별개 |
| `message` | 최대 2,000자의 운영용 메시지. URL/비밀값 제거, 원본 코드 오류는 결과 파일에서 확인 |
| `causes[]` | 바깥 예외부터 이어지는 원인 체인, 최대 8개 |
| `causes[].exception_type` | 관측된 Python 예외 유형, 최대 128자 |
| `causes[].message` | 동일한 비밀값 제거 정책의 메시지, 최대 2,000자 |
| `causes[].errno` | OSError가 제공한 OS 오류 번호, 아니면 null |
| `causes_truncated` | 체인이 8개를 넘거나 순환해 더 이상 수록하지 않았는지 |

대표 코드:

- `OUTPUT_MESSAGE_SIZE_LIMIT_EXCEEDED`, `OUTPUT_DATA_RATE_LIMIT_EXCEEDED`,
  `OUTPUT_MESSAGE_RATE_LIMIT_EXCEEDED`
- `STEP_TIMEOUT`, `OPERATION_TIMEOUT`, `CODE_EXECUTION_FAILED`
- `PERMISSION_DENIED`, `FILE_NOT_FOUND`, `OS_ERROR`, `TIMEOUT`
- `RUNTIME_UNAVAILABLE`, `INTERNAL_ERROR`

`code`는 관측된 예외 기준이고, `causes`가 하위 원인을 보완한다. 예를 들어
`RUNTIME_UNAVAILABLE` 아래 `ConnectionResetError`와 errno가 있을 수 있다.
임의의 Exception 텍스트나 SQL 파라미터, 토큰, 입력 코드, 출력 이미지, 전체 traceback은
DB/API에 복제하지 않는다. Stack 위치는 기존 `runtime.failure` 로그에서 확인한다.

대표 단계는 `RUNTIME_EXECUTE`, `RUNTIME_TIMEOUT`, `RESULT_PREPARE`,
`RESULT_APPEND`, `RESULT_FINALIZE`, `RESULT_FAILURE_SAVE`, `NOTEBOOK_BUILD`,
`NOTEBOOK_WRITE`, `ARTIFACT_REGISTER`, `NOTEBOOK_ARTIFACT_REGISTER`,
`RUNTIME_ABORT`, `RUNTIME_ABORT_RESULT`, `RUNTIME_DELETE_AFTER_ABORT`,
`RUNTIME_INTERRUPT`, `RUNTIME_DELETE`, `EXECUTION_RUN`이다.

## 소비 방법과 주의점

1. 기존 실행/Operation 완료 이벤트와 조회 API로 상태를 판단한다.
2. 실패 사유를 조사하거나 노트북/정리 상태가 비정상이면 진단 이력을 조회한다.
3. 실행 오류와 후속 `NOTEBOOK`/`CLEANUP` 오류를 별도로 확인한다. 뒤의 오류가
   원래 오류를 덮어쓰지 않는다.
4. 코드/텍스트/이미지의 실제 내용은 기존 `result_ref`가 가리키는 파일에서 읽는다.

진단은 사건마다 정확히 하나인 Incident가 아니라, 처리 경계별 관측 이력이다.
같은 장애가 서로 다른 phase로 여러 번 보일 수 있다. 재시도 성공 후에도 이전
Attempt의 오류는 보존된다. 이번 작업은 노트북/아티팩트의 필수 완료 정책을
변경하지 않으므로 Execution이 성공했어도 별도 전달·정리 오류가 남을 수 있다.

진단이 없다는 사실만으로 정상임을 증명할 수는 없다. DB 장애, 프로세스 강제 종료,
소유권 상실 또는 아직 연결하지 않은 복구 경로에서는 DB 기록이 없을 수 있다.
기존 상태/오류 필드와 `runtime.failure` 로그를 함께 확인한다.
DB 기록 실패는 `DIAGNOSTIC_PERSIST` 단계 로그로 남긴다.

## 배포

실제 2026-08-31 `0001` baseline DB에서 다음 명령을 실행한다. 데이터 초기화나
Redis 초기화는 필요 없다. 기존의 더 오래된 동명 baseline과 혼동하지 않는다.

```bash
uv run alembic upgrade head
uv run alembic current
uv run alembic check
```

새 head는 `0002`다. 새 애플리케이션보다 마이그레이션을 먼저 적용한다.
이번 작업에서는 개발자의 기존 서비스 DB를 자동으로 변경하지 않았다.
