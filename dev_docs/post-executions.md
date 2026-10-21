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
| `tenant_id` | X | 이벤트를 받을 시스템(tenant) ID. 지정하면 이 Execution의 Redis 이벤트가 전용 Stream으로 간다. 아래 [`tenant_id`](#tenant_id) 참고 |
| `lifecycle` | O | SINGLE/MULTI 실행 생명주기 설정 |
| `trigger` | O | 실행을 발생시킨 유형과 주체 |
| `runtime` | O | 필요한 Runtime 종류와 프로파일 |
| `context` | O | Agent 도메인 객체와의 추적 정보 |
| `operation` | O | 최초 Operation과 실행 Step |
| `metadata` | X | Execution 전체에 저장할 자유 형식 metadata. 기본값 `{}` |

### `tenant_id`

`tenant_id`는 요청 최상위 선택 필드이며 `context` 안에 넣지 않는다.

- 형식: `^[a-z][a-z0-9-]{1,62}[a-z0-9]$` (소문자로 시작, 소문자·숫자·`-`, 3~64자, `-`로 끝나지 않음)
- 예약어: `dlq`, `work`. Executor와 Agent가 쓰는 Stream 이름(`executor.events.dlq` 등)과
  겹치지 않게 한다.
- 형식 위반·예약어는 `422` `REQUEST_VALIDATION_ERROR`다.
- 값은 바뀌지 않는다. 후속 Operation 추가, 재시도, 취소, finalize는 받은 값이 아니라
  Execution에 저장된 `tenant_id`를 따른다. 해당 요청에는 `tenant_id` 필드가 없다.
- 멱등성 fingerprint에 포함된다. 같은 `idempotency_key`로 `tenant_id`만 바꿔 보내면 `409`다.
- 이벤트 Stream: `tenant_id`가 있으면 `{REDIS_EVENT_STREAM}.{tenant_id}`
  (기본 `executor.events.{tenant_id}`), 없으면 기존 공용 `REDIS_EVENT_STREAM`이다.
  구독 방법은 [Redis Execution 이벤트](redis-execution-events.md)를 따른다.

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

- `BATCH` trigger는 `USER` 또는 `BATCH` actor를 허용한다. 사용자가 등록한 배치는
  `USER` actor(등록 사용자 ID)로, 사용자와 무관한 시스템 배치는 `BATCH` actor로 제출한다.
- `INTERACTIVE` trigger는 `AGENT` 또는 `USER` actor를 허용한다.
- actor가 `USER`이면 trigger와 관계없이 `actor.id`와 `context.user_id`가 같아야 한다.
- Runtime Pool은 요청으로 직접 받지 않고 `INTERACTIVE`/`BATCH`에서 내부 결정한다.

### `runtime`

| 필드 | 필수 | 의미 |
|---|---:|---|
| `type` | O | Runtime provider 유형. 현재 지원 값은 `JUPYTER` |
| `profile` | O | 실행 환경 프로파일. 기본 허용값은 `default`, `3102311`. 실제 배포의 `RUNTIME_ALLOWED_PROFILES` 및 Target 지원 프로파일과 일치해야 함 |

실제 Runtime target과 session은 Worker가 실행을 할당할 때 결정되므로 제출 요청에는 넣지
않는다.

### `context`

| 필드 | 필수 | 의미 |
|---|---:|---|
| `user_id` | O | 작업 소유 사용자 ID |
| `task_id` | X | Agent가 관리하는 상위 Task ID. Task 개념이 없는 시스템은 생략한다 |
| `project_id` | X | 프로젝트 ID |
| `session_id` | X | 세션 ID. 지정하려면 `project_id`도 함께 필요 |
| `workflow_id` | X | 상위 서비스에 등록된 재사용 Workflow ID. 배치·회차 식별자는 `metadata`와 `task_id`에 둔다 |

`project_id` 또는 `session_id`가 없으면 Executor는 실제 workspace 경로를 만들 때 해당 구간에
예약값 `unscoped`를 사용한다. 요청 값으로 직접 `unscoped`를 보내는 것은 금지한다.

### `operation`

| 필드 | 필수 | 의미 |
|---|---:|---|
| `operation_timeout_seconds` | X | Worker가 Operation을 RUNNING으로 전환한 시각부터 계산하는 시간 예산. 정수 1초 이상. 생략/null이면 이 제한 없음 |
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
| `step_timeout_seconds` | X | Step의 런타임 실행·출력 수집·결과 저장 처리 제한시간. 정수 1초 이상. 생략/null이면 이 제한 없음 |
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
| `path` | O | `INPUT_STORAGE_ROOT` 기준 상대경로 (미지정 시 `SHARED_STORAGE_ROOT`). `requests/` 자동 추가 없음. 절대경로와 입력 루트 이탈 금지 |
| `sha256` | O | 파일 내용의 SHA-256. 64자리 16진수 |

PATH 파일은 `.py`, UTF-8이어야 하며 요청한 checksum과 실제 파일 checksum이 같아야 한다.
INLINE/PATH 최대 크기와 Operation/Execution 최대 Step 수는 Executor 설정을 따른다.

`lineage` 필드:

| 필드 | 필수 | 의미 |
|---|---:|---|
| `skill_name` | X | 해당 Tool이 속한 Skill 이름 |
| `tool_name` | X | 실행 Tool 이름 |
| `input_parameters` | X | Tool 입력 파라미터. 기본값 `{}` |

## Request 예시 — INLINE

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
  "runtime": {"type": "JUPYTER", "profile": "default"},
  "tenant_id": "analytics-agent",
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

위 요청의 이벤트는 `executor.events.analytics-agent` Stream으로 발행된다.

## Request 예시 — task_id 없는 BATCH

```json
{
  "idempotency_key": "batch-daily-report-2026-10-21",
  "tenant_id": "pr-batch",
  "lifecycle": {"operation_mode": "SINGLE"},
  "trigger": {
    "type": "BATCH",
    "actor": {"type": "USER", "id": "user-100"}
  },
  "runtime": {"type": "JUPYTER", "profile": "default"},
  "context": {"user_id": "user-100"},
  "operation": {
    "spec": {
      "schema_version": "1.0",
      "steps": [
        {
          "sequence": 0,
          "payload": {
            "type": "PYTHON_EXECUTE",
            "source": {"type": "INLINE", "content": "print('daily')"}
          }
        }
      ]
    }
  },
  "metadata": {"schedule_id": "daily-report", "run_date": "2026-10-21"}
}
```

## Request 예시 — PATH

Step마다 실행할 `.py` 파일 경로와 checksum을 전달한다. 하나의 PATH 파일 내용
전체가 하나의 Step으로 실행되며, 파일 내부의 함수별로 자동 분해하지 않는다.
아래는 두 Step을 실행한 뒤 후속 Operation을 기다리는 MULTI 예시다.

Executor 설정이 `INPUT_STORAGE_ROOT=/mnt/data/agent`라면 입력 파일은 다음과 같다.
Agent는 같은 저장소를 자신의 마운트 경로에서 작성하고, 요청에는 루트 기준
상대경로만 넣는다. `requests/` 같은 접두사는 필요 없다.

```text
/mnt/data/agent/
└── tasks/task-100/
    ├── prepare.py
    └── summarize.py
```

`tasks/task-100/prepare.py`:

```python
values = [10, 20, 30]
```

`tasks/task-100/summarize.py`:

```python
total = sum(values)
print(total)
```

아래 checksum은 위 코드에 각각 마지막 줄바꿈 LF 하나를 포함하고 UTF-8, BOM 없이
저장한 파일 기준이다. 실제로는 작성한 파일의 바이트로 SHA-256을 계산해 넣는다.
파일 내용이나 줄바꿈이 바뀌면 checksum도 다시 계산해야 한다.

```json
{
  "idempotency_key": "submit-task-100-path-1",
  "lifecycle": {
    "operation_mode": "MULTI",
    "operation_wait_timeout_seconds": 600
  },
  "trigger": {
    "type": "INTERACTIVE",
    "actor": {"type": "AGENT", "id": "analytics-agent"}
  },
  "runtime": {"type": "JUPYTER", "profile": "default"},
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
            "source": {
              "type": "PATH",
              "path": "tasks/task-100/prepare.py",
              "sha256": "54aed2d85ef666ce4f9a52703d69ed96cb281cd859b93c85ee04391247c60cb5"
            }
          },
          "step_timeout_seconds": 300
        },
        {
          "sequence": 1,
          "payload": {
            "type": "PYTHON_EXECUTE",
            "source": {
              "type": "PATH",
              "path": "tasks/task-100/summarize.py",
              "sha256": "ed7de6fea8894809ed9d90b0539ce374d773b67a15c57a2f9da80e85fa53549c"
            }
          },
          "step_timeout_seconds": 300
        }
      ]
    }
  }
}
```

SINGLE로 실행을 끝내려면 위 `lifecycle`을
`{"operation_mode":"SINGLE"}`로 바꾼다. PATH 지정 방식은 동일하다.
MULTI 후속 요청은 [Operation 문서의 PATH 예시](post-execution-operations.md)를
참고한다.

체크섬 계산 예시(에이전트가 실제로 작성한 파일을 지정):

```python
import hashlib
from pathlib import Path

path = Path("/mnt/data/agent/tasks/task-100/prepare.py")
print(hashlib.sha256(path.read_bytes()).hexdigest())
```

입력 파일은 요청을 제출하기 전에 작성을 완료해야 한다. 이 경로는 익스큐터가 읽는
입력 파일의 경로이며, 주피터 저장소의 파일 경로가 아니다. `INPUT_STORAGE_ROOT`를
설정하지 않으면 `SHARED_STORAGE_ROOT`를 기준으로 해석한다.

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

## 시간 제한과 장기 실행

모든 요청 시간값은 초 단위다. 예시의 300/600은 기본값이 아니다.
각 Step에는 Step 제한과 Operation 잔여 예산 중 작은 값이 적용된다.
현재 Operation 제한은 각 Step 실행 시 검사하며, 준비·마지막 후처리 전체를
독립적인 타이머로 감싸지는 않는다. 제한시간에 중단·정리 완료까지 보장하지 않는다.

별도 서비스 전체 제한 `EXECUTION_MAX_RUNTIME_SECONDS`는 기본 5일이며 최초
Worker 실행 시작부터 계산한다. 최초 큐 대기에는 별도 시간 제한이 없다.
MULTI 후속 요청 대기시간은 Operation 완료 후 시작하므로 코드 실행시간과 다르다.
Agent 생존 heartbeat나 Agent 장애만을 이유로 한 자동 취소는 제공하지 않는다.
상세한 현재 동작과 재시도 규칙은 [시간 제한 가이드](execution-timeouts.md)를 따른다.
