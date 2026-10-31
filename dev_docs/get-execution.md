# Execution 상태조회 API

## API

```http
GET /api/v1/executions/{execution_id}
```

Redis 이벤트 누락, Agent 재시작, 명령 처리 확인 등에 사용하는 PostgreSQL 원본 상태조회
API다. `execution_id`는 Execution 제출 응답에서 받은 UUID다.

제출과 같은 인증 헤더(`X-Api-Key`, `X-User-Id` 등)를 보낸다. 호출자의 tenant가 아니거나,
`X-User-Id`를 보냈는데 소유자가 다르면 `404`다. `X-User-Id` 없이 보내면 시스템 조회로 보고
tenant 안의 모든 Execution을 볼 수 있다.

## Response

### 기본 및 감사 필드

| 필드 | 의미 |
|---|---|
| `execution_id` | 조회한 Execution ID |
| `trigger_type` | 실행 유입 유형: `INTERACTIVE` 또는 `BATCH`. `trigger.type`과 같은 값이며 호환을 위해 유지 |
| `trigger` | 제출 trigger. `type`과 제출 actor `actor{type, id}`. actor는 `created_by_type`/`created_by`와 같고 제출 후 바뀌지 않음. actor 없이 제출됐다면 `null` |
| `tenant_id` | Execution의 tenant ID. 바뀌지 않으며, 이 Execution의 이벤트가 발행되는 Stream(`{REDIS_EVENT_STREAM}.{tenant_id}`)을 결정한다 |
| `metadata` | 제출 요청의 최상위 `metadata` 객체. 키 이름에 token/secret/password/credential이 들어간 값은 `[REDACTED]`로 가린다 |
| `created_by_type`, `created_by` | Execution 생성 주체 유형과 ID |
| `updated_by_type`, `updated_by` | 마지막 상태 변경 주체 유형과 ID. 예: 취소 요청 actor |
| `created_at`, `updated_at` | 생성 및 마지막 변경 시각 |

### `context`

| 필드 | 의미 |
|---|---|
| `user_id` | 작업 소유 사용자 ID |
| `project_id` | 프로젝트 ID 또는 `null` |
| `session_id` | 세션 ID 또는 `null` |

### `runtime`

| 필드 | 의미 |
|---|---|
| `type` | Runtime provider 유형. 현재 `JUPYTER` |
| `pool` | 내부 선택 Pool: `INTERACTIVE` 또는 `BATCH` |
| `profile` | 요청한 Runtime 프로파일 |
| `target_id` | 실제 할당된 Runtime target ID. 할당 전 또는 FROM_START 재시도 준비 시 null일 수 있음. 커널 정리만으로 반드시 지워지지는 않으며 Target purge 후에도 과거 ID를 유지할 수 있음 |
| `session_id` | 실제 Runtime session/kernel 식별자. 생성 전 또는 정리 후에는 `null` |

### `state`

| 필드 | 의미 |
|---|---|
| `status` | `QUEUED`, `RUNNING`, `WAITING_FOR_OPERATION`, `FINALIZING`, `CANCEL_REQUESTED`, `CANCELLED`, `SUCCEEDED`, `FAILED` |
| `version` | 상태 갱신 버전. MULTI의 operations/finalize 요청에는 이 값을 `expected_version`으로 전달 |
| `cancellation_reason` | 취소 요청 사유. 취소하지 않았다면 `null` |

### `workspace`

| 필드 | 의미 |
|---|---|
| `path` | Runtime workspace의 논리 상대경로. 준비 전에는 `null` |
| `notebook_path` | 생성된 Runtime notebook의 논리 상대경로. 준비 전에는 `null` |
| `notebook_projection.status` | `NOT_STARTED`, `PENDING`, `SUCCEEDED`, `FAILED` |
| `notebook_projection.attempt_count` | Notebook 반영 시도 횟수 |
| `notebook_projection.error_message` | Notebook 반영 실패 메시지 또는 `null` |
| `notebook_projection.projected_at` | 마지막 성공 반영 시각 또는 `null` |

### `failure`

| 필드 | 의미 |
|---|---|
| `failure` | 실패하지 않았거나 아직 실패가 확정되지 않으면 `null` |
| `failure.type` | 실패 분류: `TOOL_ERROR`, `INFRASTRUCTURE_ERROR`, `WORKER_SHUTDOWN`, `RUNTIME_UNAVAILABLE`, `LEASE_EXPIRED`, `INTERNAL_ERROR`, `OPERATION_WAIT_TIMEOUT`, `OPERATION_TIMEOUT`, `STEP_TIMEOUT`, `EXECUTION_TIMEOUT`, `OUTPUT_LIMIT_EXCEEDED`, `RUNTIME_SESSION_LOST`, `COMPLETION_FAILED` |
| `failure.message` | 실패 상세 메시지 |

### `retry`

`COMPLETION_FAILED`는 필수 결과 전달/최종 정리 실패이며 `NOT_RETRYABLE`이다.
코드가 성공한 Step과 해당 result_ref는 유지한다. 원인은 diagnostics에서 확인한다.

| 필드 | 의미 |
|---|---|
| `strategy` | `NOT_RETRYABLE`, `FROM_FAILED_STEP`, `FROM_START` |
| `count` | 지금까지 명시적으로 요청된 retry 횟수 |
| `from_sequence` | 다음 retry가 시작할 Step sequence 또는 `null` |
| `retained_runtime_session_until` | 실패한 Runtime session을 재사용할 수 있는 만료시각 또는 `null` |

`FROM_FAILED_STEP`은 보존된 Runtime session에서 실패 Step부터 이어서 실행한다.
`FROM_START`는 새 Runtime session에서 첫 Step부터 다시 실행한다.

### `recovery`

| 필드 | 의미 |
|---|---|
| `count` | Worker 장애 등으로 Execution 소유권이 복구된 횟수 |
| `runtime_session_cleanup_status` | `NOT_REQUIRED`, `PENDING`, `SUCCEEDED`, `FAILED` |
| `runtime_abort_status` | `NOT_REQUIRED`, `PENDING`, `IDLE_CONFIRMED`, `SESSION_DELETED`, `SESSION_MISSING`, `FAILED` |

cleanup은 버려진 Runtime session 정리 상태이고, abort는 취소·중단 시 Runtime 실행 중지 및
session 처리 상태다.

`CANCELLED`라도 cleanup이 `FAILED`일 수 있다. terminal 상태만으로 커널 정리 성공을
추론하지 않는다. `AVAILABLE` 노트북 Artifact 역시 Execution 성공을 뜻하지 않는다.

### `deadlines`

| 필드 | 의미 |
|---|---|
| `operation_wait_expires_at` | MULTI가 다음 Operation을 기다리는 만료시각. 대기 중이 아니면 `null` |
| `execution_expires_at` | Execution 최대 실행시간 만료시각. 시작 전에는 `null`일 수 있음 |

### `lifecycle`

| 필드 | 의미 |
|---|---|
| `operation_mode` | `SINGLE` 또는 `MULTI` |
| `operation_wait_timeout_seconds` | MULTI 다음 Operation 대기 제한시간. SINGLE은 `null` |
| `started_at` | Execution 최초 실행 시작 시각 또는 `null` |
| `finished_at` | terminal 종료 시각 또는 `null` |

시각은 UTC 기반이다. `updated_at`은 마지막 출력 시각이 아니고, Attempt 상세의
heartbeat는 Worker 소유권 갱신 시각이지 코드 진척도가 아니다.
`started_at`은 최초 Worker 실행 시작으로, 준비·MULTI 대기·재시도 간격을 포함한
경과시간과 순수 계산시간을 혼동하지 않는다. 타임아웃 기본값과 실제 범위는
[시간 제한 가이드](execution-timeouts.md)를 참고한다.

## Agent 사용 원칙

- 정상 흐름은 Redis 이벤트를 기다린다.
- 이벤트 timeout, sequence 누락, Agent 재시작 시 이 API로 상태를 복구한다.
- MULTI가 `WAITING_FOR_OPERATION`이면 응답의 최신 `state.version`을 operations 또는
  finalize 요청의 `expected_version`으로 사용한다.
- `SUCCEEDED`, `FAILED`, `CANCELLED`는 terminal 상태다.

## 목록 조회

```http
GET /api/v1/executions?project_id=project-100
X-Api-Key: <key>
X-User-Id: user-100
```

목록 API(`GET /api/v1/executions`, MCP `execution_list`)는 항상 호출자의 tenant로 한정되고,
`X-User-Id`를 보내면 그 사용자의 Execution만 돌려준다. 각 항목에도 `tenant_id`가 있다.
추가 필터 `project_id`, `session_id`, `trigger_type`, `status`는 모두 선택이고 함께 쓰면 AND로
묶는다. `user_id`, `tenant_id` 쿼리 파라미터는 없다.
