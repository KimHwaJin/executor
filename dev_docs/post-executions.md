# Execution 제출 API

## API

```http
POST /api/v1/executions
```

새 비동기 Execution과 첫 번째 Operation을 생성한다. 응답 코드 `202 Accepted`는 요청이
저장되고 실행 대기열에 들어갔다는 뜻이며, 실행 완료를 의미하지 않는다.

## Request Body

| 필드 | 필수 | 의미 |
|---|---:|---|
| `idempotency_key` | O | 호출자가 생성하는 멱등성 키. 같은 키와 같은 요청은 같은 Execution을 반환하며, 같은 키로 다른 요청을 보내면 `409` |
| `lifecycle` | O | SINGLE/MULTI 실행 생명주기 설정 |
| `trigger` | O | 실행을 발생시킨 유형과 주체 |
| `runtime` | O | 필요한 Runtime 종류와 프로파일 |
| `context` | O | Agent 도메인 객체와의 추적 정보 |
| `operation` | O | 최초 Operation과 실행 Step |
| `metadata` | X | Execution 전체에 저장할 자유 형식 metadata. 기본값 `{}` |

### `lifecycle`

| 필드 | 필수 | 의미 |
|---|---:|---|
| `operation_mode` | O | `SINGLE` 또는 `MULTI` |
| `operation_wait_timeout_seconds` | 조건부 | MULTI에서 다음 Operation을 기다릴 최대 시간. 최소 30초. MULTI에서는 필수이며 SINGLE에서는 보내면 안 됨 |

- `SINGLE`: 제출한 Operation을 끝까지 실행한 뒤 Execution을 종료한다.
- `MULTI`: 제출한 Operation을 완료하면 `WAITING_FOR_OPERATION`으로 전환하여 Agent의 다음
  Operation 또는 finalize 요청을 기다린다.

### `trigger`

| 필드 | 필수 | 의미 |
|---|---:|---|
| `type` | O | `INTERACTIVE` 또는 `BATCH`. Executor가 사용할 Runtime Pool도 이 값으로 결정 |
| `actor.type` | O | 호출 주체 유형: `AGENT`, `USER`, `BATCH` |
| `actor.id` | O | 호출 주체 식별자. 1~255자 |

규칙:

- `BATCH` trigger는 `BATCH` actor만 허용한다.
- `INTERACTIVE` trigger는 `AGENT` 또는 `USER` actor를 허용한다.
- actor가 `USER`이면 `actor.id`와 `context.user_id`가 같아야 한다.
- Runtime Pool은 요청으로 직접 받지 않고 `INTERACTIVE`/`BATCH`에서 내부 결정한다.

### `runtime`

| 필드 | 필수 | 의미 |
|---|---:|---|
| `type` | O | Runtime provider 유형. 현재 지원 값은 `JUPYTER` |
| `profile` | O | 실행 환경 프로파일. 현재 설정에 등록된 `basic`, `ml` 등의 이름 |

실제 Runtime target과 session은 Worker가 실행을 할당할 때 결정되므로 제출 요청에는 넣지
않는다.

### `context`

| 필드 | 필수 | 의미 |
|---|---:|---|
| `user_id` | O | 작업 소유 사용자 ID |
| `task_id` | O | Agent가 관리하는 상위 Task ID |
| `project_id` | X | 프로젝트 ID |
| `session_id` | X | 세션 ID. 지정하려면 `project_id`도 함께 필요 |
| `workflow_id` | X | 배치 또는 재사용 Workflow ID |

`project_id` 또는 `session_id`가 없으면 Executor는 실제 workspace 경로를 만들 때 해당 구간에
예약값 `unscoped`를 사용한다. 요청 값으로 직접 `unscoped`를 보내는 것은 금지한다.

### `operation`

| 필드 | 필수 | 의미 |
|---|---:|---|
| `operation_timeout_seconds` | X | Operation에 포함된 모든 Step의 전체 제한시간. 1초 이상 |
| `spec` | O | ExecutionSpec 1.0과 Step 목록 |
| `metadata` | X | 이 Operation에만 적용되는 자유 형식 metadata. 기본값 `{}` |

### `operation.spec`

| 필드 | 필수 | 의미 |
|---|---:|---|
| `schema_version` | O | 현재 고정값 `1.0` |
| `steps` | O | 하나 이상의 실행 Step. 최초 제출은 sequence `0`부터 연속·오름차순이어야 함 |

### `operation.spec.steps[]`

| 필드 | 필수 | 의미 |
|---|---:|---|
| `sequence` | O | Execution 전체 기준 Step 순서. 최초 Step은 `0` |
| `payload.type` | O | 현재 고정값 `PYTHON_EXECUTE` |
| `payload.source` | O | 실행할 Python 코드의 INLINE 또는 PATH source |
| `step_timeout_seconds` | X | 해당 Step만의 제한시간. 1초 이상 |
| `lineage` | X | Skill, Tool 및 입력 파라미터 추적 정보 |

INLINE source:

| 필드 | 필수 | 의미 |
|---|---:|---|
| `type` | O | `INLINE` |
| `content` | O | 실행할 Python 코드. 공백일 수 없음 |

PATH source:

| 필드 | 필수 | 의미 |
|---|---:|---|
| `type` | O | `PATH` |
| `path` | O | Agent/Executor 입력 공유 루트 기준 상대경로. 절대경로와 상위 디렉터리 이탈 금지 |
| `sha256` | O | 파일 내용의 SHA-256. 64자리 16진수 |

PATH 파일은 `.py`, UTF-8이어야 하며 요청한 checksum과 실제 파일 checksum이 같아야 한다.
INLINE/PATH 최대 크기와 Operation/Execution 최대 Step 수는 Executor 설정을 따른다.

`lineage` 필드:

| 필드 | 필수 | 의미 |
|---|---:|---|
| `skill_name` | X | 해당 Tool이 속한 Skill 이름 |
| `tool_name` | X | 실행 Tool 이름 |
| `input_parameters` | X | Tool 입력 파라미터. 기본값 `{}` |

## Request 예시

```json
{
  "idempotency_key": "submit-task-100-attempt-1",
  "lifecycle": {
    "operation_mode": "MULTI",
    "operation_wait_timeout_seconds": 600
  },
  "trigger": {
    "type": "INTERACTIVE",
    "actor": {"type": "AGENT", "id": "analytics-agent"}
  },
  "runtime": {"type": "JUPYTER", "profile": "basic"},
  "context": {
    "user_id": "user-100",
    "task_id": "task-100",
    "project_id": "project-100",
    "session_id": "session-100"
  },
  "operation": {
    "operation_timeout_seconds": 600,
    "spec": {
      "schema_version": "1.0",
      "steps": [
        {
          "sequence": 0,
          "payload": {
            "type": "PYTHON_EXECUTE",
            "source": {"type": "INLINE", "content": "print('hello')"}
          },
          "step_timeout_seconds": 300,
          "lineage": {
            "skill_name": "eda",
            "tool_name": "preview",
            "input_parameters": {}
          }
        }
      ]
    },
    "metadata": {}
  },
  "metadata": {}
}
```

## Response: `202 Accepted`

| 필드 | 의미 |
|---|---|
| `execution_id` | 생성된 Execution ID |
| `operation.operation_id` | 함께 생성된 최초 Operation ID |
| `operation.steps[].sequence` | 접수된 Step sequence |
| `operation.steps[].step_id` | Executor가 발급한 논리 Step ID |
| `state.status` | 접수 직후 상태. 일반적으로 `QUEUED` |
| `state.version` | 현재 Execution 상태 버전 |
| `created_by_type`, `created_by` | 생성 주체 유형과 ID |
| `updated_by_type`, `updated_by` | 마지막 변경 주체 유형과 ID |
| `created_at`, `updated_at` | 생성 및 마지막 변경 시각 |

응답 `Location` 헤더는 `/api/v1/executions/{execution_id}`를 가리킨다. 이후 Redis 이벤트를
기다리고, 필요할 때 Location의 상태조회 API로 PostgreSQL 원본 상태를 확인한다.

