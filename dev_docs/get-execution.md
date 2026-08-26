# Execution 상태조회 API

## API

```http
GET /api/v1/executions/{execution_id}
```

Redis 이벤트 누락, Agent 재시작, 명령 처리 확인 등에 사용하는 PostgreSQL 원본 상태조회
API다. `execution_id`는 Execution 제출 응답에서 받은 UUID다.

## Response

### 기본 및 감사 필드

| 필드 | 의미 |
|---|---|
| `execution_id` | 조회한 Execution ID |
| `trigger_type` | 실행 유입 유형: `INTERACTIVE` 또는 `BATCH` |
| `created_by_type`, `created_by` | Execution 생성 주체 유형과 ID |
| `updated_by_type`, `updated_by` | 마지막 상태 변경 주체 유형과 ID |
| `created_at`, `updated_at` | 생성 및 마지막 변경 시각 |

### `context`

| 필드 | 의미 |
|---|---|
| `user_id` | 작업 소유 사용자 ID |
| `task_id` | Agent의 상위 Task ID |
| `project_id` | 프로젝트 ID 또는 `null` |
| `session_id` | 세션 ID 또는 `null` |
| `workflow_id` | Workflow ID 또는 `null` |

### `runtime`

| 필드 | 의미 |
|---|---|
| `type` | Runtime provider 유형. 현재 `JUPYTER` |
| `pool` | 내부 선택 Pool: `INTERACTIVE` 또는 `BATCH` |
| `profile` | 요청한 Runtime 프로파일 |
| `target_id` | 실제 할당된 Runtime target ID. 할당 전 또는 정리 후에는 `null` |
| `session_id` | 실제 Runtime session/kernel 식별자. 생성 전 또는 정리 후에는 `null` |

### `state`

| 필드 | 의미 |
|---|---|
| `status` | `QUEUED`, `DISPATCHED`, `RUNNING`, `WAITING_FOR_OPERATION`, `FINALIZING`, `CANCEL_REQUESTED`, `CANCELLED`, `SUCCEEDED`, `FAILED` |
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
| `failure.type` | 실패 분류: `TOOL_ERROR`, `INFRASTRUCTURE_ERROR`, `WORKER_SHUTDOWN`, `RUNTIME_UNAVAILABLE`, `LEASE_EXPIRED`, `INTERNAL_ERROR`, `OPERATION_WAIT_TIMEOUT`, `OPERATION_TIMEOUT`, `STEP_TIMEOUT`, `EXECUTION_TIMEOUT`, `OUTPUT_LIMIT_EXCEEDED`, `RUNTIME_SESSION_LOST` |
| `failure.message` | 실패 상세 메시지 |

### `retry`

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

## Agent 사용 원칙

- 정상 흐름은 Redis 이벤트를 기다린다.
- 이벤트 timeout, sequence 누락, Agent 재시작 시 이 API로 상태를 복구한다.
- MULTI가 `WAITING_FOR_OPERATION`이면 응답의 최신 `state.version`을 operations 또는
  finalize 요청의 `expected_version`으로 사용한다.
- `SUCCEEDED`, `FAILED`, `CANCELLED`는 terminal 상태다.

