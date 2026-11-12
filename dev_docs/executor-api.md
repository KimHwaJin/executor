# Executor API 연동 가이드

Agent, BFF, 배치 서비스가 Executor를 호출할 때 읽는 단일 문서다. 모든 API의 경로, 헤더,
요청·응답 필드와 의미, 호출 순서를 담는다. 발행되는 Redis 이벤트는
[Executor 이벤트](executor-events.md)를 본다.

- 기준 경로: `/api/v1`. MCP는 같은 서버의 `/mcp`(Streamable HTTP)이며 같은 헤더를 쓴다.
- 명령 API는 `202 Accepted`를 돌려준다. 요청이 저장됐다는 뜻이지 실행이 끝났다는 뜻이 아니다.
  실제 실행은 Worker가 비동기로 하고, 진행은 Redis 이벤트와 조회 API로 확인한다.
- 시각은 모두 UTC ISO-8601, 시간 값은 모두 초 단위 정수다.
- 전체 스키마: [Execution ReDoc](execution-api-redoc.html),
  [제출 API ReDoc](post-executions-redoc.html). 개발계 서버는 `/docs`, `/redoc`,
  `/openapi`도 제공한다(stg·prd는 `DOCS_ENABLED=false`라 404).

## 1. 식별자는 어디로 보내나

값의 역할에 따라 위치가 정해진다. 한 값을 두 곳으로 받지 않는다.

| 역할 | 위치 | 값 |
| --- | --- | --- |
| 누가, 어느 tenant·프로젝트·세션을 위해 호출하나 | **헤더** | `X-Api-Key`, `X-Tenant-Id`, `X-User-Id`, `X-Project-Id`, `X-Session-Id` |
| 어느 리소스에 대한 요청인가 | **경로** | `execution_id`, `operation_id`, `attempt_id`, `step_id`, `artifact_id`, `target_id`, `run_id`, `cell_index` |
| 목록에서 무엇을 고르나 | **쿼리** | `project_id`, `session_id`, `status`, `trigger_type`, `cursor`, `limit` 등 |
| 무엇을 할 것인가 | **바디** | `idempotency_key`, `expected_version`, `spec`, `reason`, `metadata` 등 |

- 바디에 `actor`, `context`, `tenant_id`, `user_id`를 넣지 않는다. 모든 요청 모델이
  모르는 필드를 거부하므로 넣으면 `422 REQUEST_VALIDATION_ERROR`다.
- "누가 했나"는 헤더에서 정해진다. `X-User-Id`가 `created_by`/`updated_by`로,
  API key의 client 이름(`AUTH_CLIENTS[].name`, 예: `pr-agentic-service`, `pr-batch`)이
  `created_by_client`/`updated_by_client`로 기록된다.
- `X-Project-Id`/`X-Session-Id`는 **새 Execution이 속할 곳**이다. 제출 때만 저장되고,
  다른 API에서는 형식만 검사하고 쓰지 않는다. 기존 Execution은 저장된 값을 계속 쓴다.
  목록에서 프로젝트·세션으로 고르려면 헤더가 아니라 쿼리 `project_id`, `session_id`를 쓴다.

## 2. 헤더

| 헤더 | 필수 | 규칙 |
| --- | --- | --- |
| `X-Api-Key` | 모든 호출 | `AUTH_CLIENTS`에 등록된 key. 없거나 틀리면 `401 UNAUTHENTICATED` |
| `X-Tenant-Id` | 선택 | relay key만, 그 key의 `allowed_tenants` 안에서 쓸 수 있다(`["*"]`는 전부). 없으면 key의 기본 `tenant`. 형식 `^[a-z][a-z0-9-]{1,62}[a-z0-9]$`, 예약어 `dlq`·`work` 금지. 형식 오류는 `400 INVALID_IDENTITY`, 허용 밖이면 `403 FORBIDDEN`. key에 기본 tenant도 없고 헤더도 없으면 `400` |
| `X-User-Id` | 쓰기 필수 | 행위자(사용자·운영자) ID. 소문자로 바꾼 뒤 `^[a-z0-9][a-z0-9._-]{0,63}$`. 쓰기에 없으면 `400 INVALID_IDENTITY`. 읽기에서 생략하면 시스템 조회로 tenant 전체를 본다 |
| `X-Project-Id` | 선택 | `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`, `unscoped` 금지. 제출 때만 저장 |
| `X-Session-Id` | 선택 | 형식은 `X-Project-Id`와 같고, 보내려면 `X-Project-Id`도 있어야 한다. 제출 때만 저장 |
| `X-Request-ID` | 선택 | 호출자 요청 ID. `^[A-Za-z0-9._:-]{1,128}$`면 Executor 로그에 함께 남는다 |
| `Content-Type` | 바디가 있을 때 | `application/json` |

### 보이는 범위

- Execution은 tenant가 호출자의 tenant이고, `X-User-Id`를 보냈다면 소유자(`context.user_id`)가
  그 사용자일 때만 보인다. 아니면 존재해도 `404`다. Artifact는 그 Artifact의 Execution 기준이다.
- 쓰기(POST)는 `X-User-Id`가 필수이고 그 사용자가 Execution 소유자여야 한다.
- 관리자 API(`/runtime-targets*`, `/runtime-pools`, `/maintenance*`, `/workerz`, MCP `runtime_target_*`)는
  `admin: true` key만 호출할 수 있다(`403`). 관리자 쓰기도 `X-User-Id`(운영자 ID)가 필수다.

### tenant

모든 Execution에는 tenant가 있고 바뀌지 않는다. 이 값이 이벤트 Stream
`{REDIS_EVENT_STREAM}.{tenant_id}`(기본 `executor.events.{tenant_id}`)를 정한다.
제출의 멱등성 키도 tenant 단위다.

## 3. 공통 규칙

### 멱등성

- 모든 명령 바디에 `idempotency_key`(1~255자)가 있다(probe·purge 제외).
- 제출 키는 tenant 단위, 기존 Execution에 대한 명령(cancel, retry, operations, finalize,
  artifacts) 키는 Execution 단위로 구분한다.
- 같은 키·같은 요청은 처음 결과를 다시 돌려준다. 같은 키로 다른 요청을 보내면
  `409 IDEMPOTENCY_CONFLICT`. 요청의 같음은 바디와 행위자(`X-User-Id`, client)로 판단한다.

### 버전

`state.version`은 상태가 바뀔 때마다 오른다. MULTI의 operations·finalize는
`expected_version`이 현재 값과 같아야 하며, 다르면 `409`다. 최신 값은 상태조회나
이벤트에서 얻는다.

### 감사 필드

대부분의 응답에 들어 있다.

| 필드 | 의미 |
| --- | --- |
| `created_by` | 만든 요청의 `X-User-Id`. Worker가 만든 행(Attempt, 이벤트 등)은 그 Execution의 마지막 행위자를 이어받는다 |
| `created_by_client` | 만든 요청의 API key client 이름 |
| `updated_by`, `updated_by_client` | 마지막으로 바꾼 요청의 사용자와 client. 예: 취소를 요청한 사용자 |
| `created_at`, `updated_at` | 생성·마지막 변경 시각. `updated_at`은 출력 시각이 아니다 |

0015 마이그레이션 전에 만들어진 행은 `*_by_client`가 `null`이고 `*_by`에 예전 actor ID가 남아 있다.

### 페이지

목록 응답은 `{"items": [...], "next_cursor": "...", "has_more": true}`다. 다음 페이지는
`next_cursor`를 그대로 `cursor`로 보낸다. 커서를 고치거나 필터를 바꾸면 `422 INVALID_CURSOR`다.

### 요청 크기

바디는 `REQUEST_BODY_MAX_BYTES`(기본 30MB) 이하. 넘으면 `413 REQUEST_BODY_TOO_LARGE`.
큰 코드는 PATH source로 보낸다.

### 오류

모든 오류는 `{"error": {"code", "message", "details"}}`이다. `details[]`에는 `location`
(`body`/`header`/`query`/`path` + 필드 경로), `message`, `type`, `context`가 있다. 형식 오류는
`422 REQUEST_VALIDATION_ERROR`, 규칙 위반은 `422 INVALID_EXECUTION_SPEC`, 현재 상태·버전과의
충돌은 `409`다. 코드 전체는 [오류 응답](errors.md)을 본다. MCP 오류는
`[CODE] message details=[...]` 문자열이다.

## 4. API 목록

| 메서드 · 경로 | MCP 도구 | 용도 |
| --- | --- | --- |
| `POST /executions` | `execution_submit` | Execution과 첫 Operation 제출 |
| `GET /executions` | `execution_list` | Execution 목록 |
| `GET /executions/{execution_id}` | `execution_get` | 현재 상태 |
| `GET /executions/{execution_id}/result` | `execution_result_get` | 통합 결과 인덱스 |
| `POST /executions/{execution_id}/cancel` | `execution_cancel` | 취소 요청 |
| `POST /executions/{execution_id}/retry` | `execution_retry` | 실패한 SINGLE 재시도 |
| `POST /executions/{execution_id}/operations` | `execution_operation_create` | MULTI 다음 Operation |
| `POST /executions/{execution_id}/finalize` | `execution_finalize` | MULTI 종료 |
| `GET /executions/{execution_id}/operations` | `execution_operation_list` | Operation 목록 |
| `GET /executions/{execution_id}/operations/{operation_id}` | `execution_operation_get` | Operation 상세 |
| `GET /executions/{execution_id}/operations/{operation_id}/result` | `execution_operation_result_get` | Operation 결과 |
| `GET /executions/{execution_id}/operations/{operation_id}/steps` | `execution_operation_step_list` | Operation의 Step 목록 |
| `GET /executions/{execution_id}/steps` | `execution_step_list` | Step 목록 |
| `GET /executions/{execution_id}/steps/{step_id}` | 없음 | Step 상세(결과 참조 포함) |
| `GET /executions/{execution_id}/attempts` | `execution_attempt_list` | Attempt 목록 |
| `GET /executions/{execution_id}/attempts/{attempt_id}` | `execution_attempt_get` | Attempt 상세 |
| `GET /executions/{execution_id}/attempts/{attempt_id}/steps` | `execution_attempt_step_list` | Attempt의 Step 결과 |
| `GET /executions/{execution_id}/events` | `execution_event_list` | 이벤트 이력(누락 복구) |
| `GET /executions/{execution_id}/diagnostics` | 없음 | 실패 진단 이력 |
| `GET /executions/{execution_id}/artifacts` | `execution_artifact_list` | Artifact 목록 |
| `POST /executions/{execution_id}/artifacts` | `execution_artifact_create` | 텍스트 Artifact 작성 |
| `GET /artifacts/{artifact_id}` | `execution_artifact_get` | Artifact 상세 |
| `GET /artifacts/{artifact_id}/content` | 없음 | Artifact 파일 다운로드 |
| `GET /executions/{execution_id}/notebook` | `execution_notebook_read` | 노트북 페이지 |
| `GET /executions/{execution_id}/notebook/cells/{cell_index}` | `execution_notebook_cell_read` | 노트북 셀 하나 |
| `POST /runtime-targets` | `runtime_target_upsert` | (관리자) Runtime 서버 등록·수정 |
| `GET /runtime-targets` | `runtime_target_list` | (관리자) Runtime 서버 목록 |
| `GET /runtime-targets/{target_id}` | `runtime_target_get` | (관리자) Runtime 서버 상세 |
| `POST /runtime-targets/{target_id}/probe` | `runtime_target_probe` | (관리자) 즉시 점검 |
| `POST /runtime-targets/{target_id}/drain` | `runtime_target_set_state`(`DRAINING`) | (관리자) 새 작업 배정 중지 |
| `POST /runtime-targets/{target_id}/activate` | `runtime_target_set_state`(`ACTIVE`) | (관리자) 배정 재개 |
| `POST /runtime-targets/{target_id}/disable` | `runtime_target_disable` | (관리자) 비활성화 |
| `DELETE /runtime-targets/{target_id}` | 없음 | (관리자) 등록 삭제(purge) |
| `GET /runtime-pools` | 없음 | (관리자) Pool 용량 요약 |
| `GET /maintenance` | 없음 | (관리자) Executor 수용 상태 |
| `POST /maintenance/drain`, `/maintenance/activate` | 없음 | (관리자) 전체 수용 중지·재개 |
| `POST /maintenance/runs` | 없음 | (관리자) 진행 중 실행 일괄 중지 |
| `GET /maintenance/runs/{run_id}`, `/maintenance/runs/{run_id}/targets` | 없음 | (관리자) 일괄 중지 진행 |

MCP 도구는 REST와 같은 바디를 인자로 받는다. 경로의 `execution_id`, `target_id`는 도구 인자
(`request.execution_id` 등)로 들어간다. 헤더는 MCP 연결의 HTTP 헤더로 보낸다.

## 5. 실행 흐름

```text
SINGLE: submit ─▶ (QUEUED → RUNNING) ─▶ execution.completed(SUCCEEDED|FAILED|CANCELLED)
                                         └ FAILED이고 재시도 가능하면 retry ─▶ RUNNING ─▶ ...

MULTI:  submit ─▶ RUNNING ─▶ WAITING_FOR_OPERATION ─┬ operations ─▶ QUEUED → RUNNING ─▶ WAITING_FOR_OPERATION ...
                                                     ├ finalize   ─▶ FINALIZING ─▶ SUCCEEDED|FAILED
                                                     └ (대기 기한 초과) ─▶ FAILED / OPERATION_WAIT_TIMEOUT
어느 상태든 terminal 전이면 cancel ─▶ CANCEL_REQUESTED ─▶ CANCELLED
```

1. 제출 응답의 `execution_id`를 저장한다.
2. tenant Stream에서 그 Execution의 이벤트를 기다린다. 이벤트는 깨우는 신호다.
3. Operation이나 Execution이 끝나면 `.../result` 또는 `.../operations/{id}/result`로 결과를 읽고,
   Step 결과 원본은 `result_ref`의 공유 PV 경로에서 읽는다.
4. 이벤트를 놓쳤거나 재시작했으면 `GET /executions/{id}`로 상태를, `.../events?after_sequence=`로
   누락 이벤트를 다시 읽는다.

## 6. Execution 제출 — `POST /executions`

새 Execution과 첫 Operation을 만든다. 응답 `202`, `Location: /api/v1/executions/{execution_id}`.

### 헤더 예

```http
POST /api/v1/executions
X-Api-Key: <key>
X-User-Id: user-100
X-Project-Id: project-100
X-Session-Id: session-100
Content-Type: application/json
```

relay key가 다른 tenant로 제출할 때는 `X-Tenant-Id`를 더한다. 프로젝트·세션이 없으면
workspace 경로의 그 자리에 예약값 `unscoped`가 쓰인다.

### 바디

| 필드 | 필수 | 의미 |
| --- | :---: | --- |
| `idempotency_key` | O | tenant 단위 멱등성 키 |
| `lifecycle.operation_mode` | O | `SINGLE`: 제출한 Operation을 끝내면 Execution 종료. `MULTI`: Operation이 끝나면 `WAITING_FOR_OPERATION`에서 다음 요청을 기다림 |
| `lifecycle.operation_wait_timeout_seconds` | MULTI 필수 | 다음 Operation을 기다리는 최대 시간. 30 이상. SINGLE은 보내면 `422` |
| `trigger.type` | O | `INTERACTIVE` 또는 `BATCH`. Runtime Pool이 이 값으로 정해진다 |
| `runtime.type` | O | 현재 `JUPYTER` |
| `runtime.profile` | O | 커널 프로파일(1~128자). 기본 허용값 `default`, `3102311`. 배포의 `RUNTIME_ALLOWED_PROFILES`와 Target 지원 프로파일을 따른다 |
| `operation.operation_timeout_seconds` | X | Operation이 RUNNING이 된 때부터 재는 시간 예산. 1 이상. 생략하면 제한 없음 |
| `operation.spec` | O | ExecutionSpec. 아래 참고 |
| `operation.metadata` | X | 이 Operation에만 붙는 metadata. 규칙은 최상위 `metadata`와 같다 |
| `metadata` | X | 호출자 소유 metadata(batch_id, run_id 등). 그대로 저장되고 상세 조회와 `execution.completed`로 돌려받는다. 키 32개 이하, 키 `^[a-z][a-z0-9_]{0,63}$`, 직렬화 4096 bytes 이하. 이벤트로 공개되므로 비밀값을 넣지 않는다 |

### `spec`

| 필드 | 필수 | 의미 |
| --- | :---: | --- |
| `schema_version` | O | `"1.0"` |
| `steps[]` | O | 1개 이상. 제출은 sequence `0`부터 연속·오름차순 |
| `steps[].sequence` | O | Execution 전체 기준 순서 |
| `steps[].payload.type` | O | `PYTHON_EXECUTE` |
| `steps[].payload.source` | O | INLINE `{"type":"INLINE","content":"..."}` 또는 PATH `{"type":"PATH","path":"...","sha256":"..."}` |
| `steps[].step_timeout_seconds` | X | Step 실행·출력 수집·결과 저장 제한. 1 이상. 생략하면 제한 없음 |
| `steps[].lineage.skill_name`, `tool_name` | X | 추적용 Skill·Tool 이름 |
| `steps[].lineage.input_parameters` | X | Tool 입력. 키 32개 이하, 4096 bytes 이하 |

- INLINE `content`는 공백일 수 없다.
- PATH `path`는 `STORAGE_ROOT` 기준 상대경로(절대경로·`..` 금지)인 UTF-8 `.py` 파일이고,
  `sha256`은 파일 바이트의 SHA-256(64자리 16진수)과 같아야 한다. 파일은 제출 전에 다 써 둔다.
  PATH 파일 하나가 Step 하나다.
- 한 Operation 최대 Step 수 `EXECUTION_MAX_STEPS_PER_OPERATION`(기본 100), Execution 누적
  `EXECUTION_MAX_STEPS_PER_EXECUTION`(기본 1000). 넘으면 `422 INVALID_EXECUTION_SPEC`.
- 각 Step에는 Step 제한과 Operation 잔여 예산 중 작은 값이 적용된다. Step·Operation 제한이
  Executor 전체 제한(`EXECUTION_MAX_RUNTIME_SECONDS`, 기본 7일)을 넘으면 `422`.
  세부는 [시간 제한](execution-timeouts.md).

### 예 — INTERACTIVE MULTI, INLINE

```json
{
  "idempotency_key": "submit-task-100-attempt-1",
  "lifecycle": {"operation_mode": "MULTI", "operation_wait_timeout_seconds": 600},
  "trigger": {"type": "INTERACTIVE"},
  "runtime": {"type": "JUPYTER", "profile": "default"},
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
          "lineage": {"skill_name": "eda", "tool_name": "preview", "input_parameters": {}}
        }
      ]
    }
  },
  "metadata": {"task_id": "task-100"}
}
```

### 예 — BATCH SINGLE, PATH

```http
POST /api/v1/executions
X-Api-Key: <pr-batch key>
X-Tenant-Id: dtest-agent-service
X-User-Id: user-100
```

```json
{
  "idempotency_key": "batch-run-7f3c:submit",
  "lifecycle": {"operation_mode": "SINGLE"},
  "trigger": {"type": "BATCH"},
  "runtime": {"type": "JUPYTER", "profile": "default"},
  "operation": {
    "spec": {
      "schema_version": "1.0",
      "steps": [
        {
          "sequence": 0,
          "payload": {
            "type": "PYTHON_EXECUTE",
            "source": {
              "type": "PATH",
              "path": "agent/tasks/task-100/prepare.py",
              "sha256": "54aed2d85ef666ce4f9a52703d69ed96cb281cd859b93c85ee04391247c60cb5"
            }
          }
        }
      ]
    }
  },
  "metadata": {"batch_id": "daily-report", "run_date": "2026-10-21"}
}
```

checksum 계산: `hashlib.sha256(Path(root, path).read_bytes()).hexdigest()`. 줄바꿈 하나만 달라도
값이 바뀐다.

### 응답 — 명령 접수(모든 실행 명령 공통)

```json
{
  "execution_id": "0b6c…",
  "operation": {"operation_id": "5d1e…", "steps": [{"sequence": 0, "step_id": "a8f2…"}]},
  "state": {"status": "QUEUED", "version": 0},
  "created_by": "user-100",
  "created_by_client": "pr-agentic-service",
  "updated_by": "user-100",
  "updated_by_client": "pr-agentic-service",
  "created_at": "2026-11-03T05:00:00Z",
  "updated_at": "2026-11-03T05:00:00Z"
}
```

| 필드 | 의미 |
| --- | --- |
| `execution_id` | Execution ID |
| `operation` | 이 명령으로 만들어졌거나 다시 대기열에 들어간 Operation. cancel·finalize는 `null` |
| `operation.steps[]` | 접수된 Step의 `sequence`와 Executor가 발급한 `step_id` |
| `state.status`, `state.version` | 명령 반영 직후 상태와 버전 |

## 7. Execution 목록 — `GET /executions`

| 쿼리 | 의미 |
| --- | --- |
| `user_id` | 소유자로 거른다. `X-User-Id` 형식. `X-User-Id`를 보냈다면 그 값과 같아야 하고, 다르면 `403 FORBIDDEN` |
| `project_id`, `session_id` | 프로젝트·세션으로 거른다. 헤더와 같은 형식, 어기면 `422` |
| `trigger_type` | `INTERACTIVE` 또는 `BATCH` |
| `status` | Execution 상태 |
| `cursor`, `limit` | 페이지. `limit` 1~200, 기본 100 |

항상 호출자 tenant로 한정되고, `X-User-Id`를 보내면 그 사용자의 것만 나온다. `X-User-Id` 없이 부르는 시스템 조회는 tenant 전체를 보고, 소유자로 거를 때는 쿼리 `user_id`를 쓴다. 필터는 AND다.
최신순이며 시간 필터는 없다. `items[]` 필드: `execution_id`, `operation_mode`, `trigger{type}`, `tenant_id`, `context`, `metadata`, `state{status, version}`,
`lifecycle{started_at, finished_at}`, `step_count`, 감사 필드.

## 8. 상태조회 — `GET /executions/{execution_id}`

PostgreSQL의 원본 상태다. 이벤트 누락, 재시작, 명령 반영 확인에 쓴다.

| 필드 | 의미 |
| --- | --- |
| `execution_id` | Execution ID |
| `trigger.type` | `INTERACTIVE` 또는 `BATCH`. 목록 필터 쿼리 이름은 `trigger_type`이다 |
| `tenant_id` | Execution의 tenant. 이벤트 Stream을 정한다 |
| `context.user_id` | 소유 사용자(제출 때 `X-User-Id`) |
| `context.project_id`, `context.session_id` | 제출 때 헤더 값 또는 `null` |
| `metadata` | 제출 최상위 `metadata`. 키 이름에 token/secret/password/credential이 들어간 값은 `[REDACTED]` |
| `runtime.type`, `pool`, `profile` | provider, 내부 결정 Pool(`INTERACTIVE`/`BATCH`), 요청 프로파일 |
| `runtime.target_id` | 할당된 Runtime 서버 ID. 할당 전이나 FROM_START 재시도 준비 중에는 `null`. 서버 purge 후에도 과거 ID가 남는다 |
| `runtime.kernel_id` | Jupyter 커널 ID(`/api/kernels/{id}`, 호출자 세션과 무관). 생성 전·정리 후 `null` |
| `state.status` | `QUEUED`, `RUNNING`, `WAITING_FOR_OPERATION`, `FINALIZING`, `CANCEL_REQUESTED`, `CANCELLED`, `SUCCEEDED`, `FAILED`. 끝의 셋이 terminal |
| `state.version` | 상태 버전. operations·finalize의 `expected_version` |
| `state.cancellation_reason` | 취소 사유 또는 `null` |
| `state.finalized_by` | 무엇이 끝냈나: `AGENT`(finalize), `WAIT_TIMEOUT`(MULTI 대기 기한), `MAX_RUNTIME`(전체 수명). 진행 중·SINGLE 정상 종료·취소는 `null` |
| `workspace.path`, `workspace.notebook_path` | Runtime workspace와 노트북의 상대경로. 준비 전 `null` |
| `workspace.notebook_projection` | 노트북 반영 상태 `status`(`NOT_STARTED`/`PENDING`/`SUCCEEDED`/`FAILED`), `attempt_count`, `error_message`, `projected_at`. 반영 실패는 실행 성공에 영향을 주지 않는다 |
| `failure` | `null` 또는 `{type, message}`. `type`: `TOOL_ERROR`, `INFRASTRUCTURE_ERROR`, `WORKER_SHUTDOWN`, `RUNTIME_UNAVAILABLE`, `LEASE_EXPIRED`, `INTERNAL_ERROR`, `OPERATION_WAIT_TIMEOUT`, `OPERATION_TIMEOUT`, `STEP_TIMEOUT`, `EXECUTION_TIMEOUT`, `QUEUE_TIMEOUT`, `OUTPUT_LIMIT_EXCEEDED`, `RUNTIME_SESSION_LOST`, `COMPLETION_FAILED` |
| `retry.strategy` | `NOT_RETRYABLE`, `FROM_FAILED_STEP`(보존 커널에서 실패 Step부터), `FROM_START`(새 커널에서 처음부터) |
| `retry.count`, `retry.from_sequence` | 요청된 retry 횟수, 다음 retry 시작 sequence |
| `retry.retained_runtime_session_until` | 실패 커널 보존 만료 시각 또는 `null` |
| `recovery.count` | Worker 장애로 소유권이 복구된 횟수 |
| `recovery.runtime_session_cleanup_status` | 버려진 커널 정리: `NOT_REQUIRED`, `PENDING`, `SUCCEEDED`, `FAILED` |
| `recovery.runtime_abort_status` | 취소·중단 시 커널 중지: `NOT_REQUIRED`, `PENDING`, `IDLE_CONFIRMED`, `SESSION_DELETED`, `SESSION_MISSING`, `FAILED` |
| `deadlines.operation_wait_expires_at` | MULTI 다음 Operation 대기 만료. 대기 중이 아니면 `null` |
| `deadlines.execution_expires_at` | 전체 수명 만료. 시작 전 `null` |
| `lifecycle.operation_mode`, `operation_wait_timeout_seconds` | 제출 값 |
| `lifecycle.started_at`, `finished_at` | 최초 Worker 시작, terminal 시각 |
| 감사 필드 | `updated_by`는 마지막으로 상태를 바꾼 사용자(예: 취소 요청자) |

- `CANCELLED`라도 커널 정리가 `FAILED`일 수 있다. terminal 상태만으로 커널 정리를 추론하지 않는다.
- `COMPLETION_FAILED`는 필수 결과 전달·최종 정리 실패이며 `NOT_RETRYABLE`이다. 이미 성공한 Step과
  `result_ref`는 그대로다. 원인은 diagnostics로 본다.

## 9. 결과 — `GET /executions/{execution_id}/result`

이벤트를 받은 뒤 결과를 읽는 경로다. `execution`(상태·버전), `operations[]`(각 Operation과 현재
Step 결과), `attempts[]`, `artifacts[]` 요약을 준다. Step 결과는 출력 요약과 공유 PV의
`result_ref`이며, 원문 출력은 `result_ref.relative_path`의 manifest를 공유 루트에 결합해 읽고
checksum을 검증한다. Operation 하나만 볼 때는 `GET .../operations/{operation_id}/result`
(`execution`, `operation`). 필드와 읽는 법은 [결과 조회](get-execution-result.md),
manifest는 [Step 결과 manifest](step-result-manifest.md).

## 10. 취소 — `POST /executions/{execution_id}/cancel`

| 바디 | 필수 | 의미 |
| --- | :---: | --- |
| `idempotency_key` | O | Execution 단위 키 |
| `reason` | X | 취소 사유. 2000자 이하 |

```json
{"idempotency_key": "cancel-execution-100", "reason": "사용자가 분석을 취소했습니다."}
```

- terminal 상태에는 `409`. 이미 `CANCEL_REQUESTED`면 현재 상태를 그대로 돌려준다.
- 응답 `state.status`는 보통 `CANCEL_REQUESTED`, `operation`은 `null`, `updated_by`는 취소한 사용자.
- 이후 `execution.completed`의 `payload.status`가 `CANCELLED`(또는 `FAILED`)가 된다. 실행 중이던
  Step은 `CANCELLED`, 시작 전 Step은 `SKIPPED`이며 `execution.step_completed`는 실행 중이던
  Step에만 나간다.
- `CANCELLED`가 커널 정리 성공을 뜻하지 않는다. `recovery.runtime_session_cleanup_status`를 본다.

## 11. 재시도 — `POST /executions/{execution_id}/retry`

실패한 SINGLE Execution을 같은 Execution·Operation 안에서 다시 실행한다.

| 바디 | 필수 | 의미 |
| --- | :---: | --- |
| `idempotency_key` | O | Execution 단위 키 |

- 조건: `FAILED`, `SINGLE`, `retry.strategy != NOT_RETRYABLE`. `FROM_FAILED_STEP`은 보존 커널이
  보존 기한(`FAILED_SESSION_RETENTION_SECONDS`, 기본 1시간) 안에 살아 있어야 하고, `FROM_START`는
  버려진 커널 정리가 끝나야 한다. 어기면 `409`.
- 전략과 시작 sequence는 Executor가 정한다(`retry.strategy`, `retry.from_sequence`).
- MULTI 실패는 retry 대신 고친 코드로 다음 Operation을 제출한다.
- 응답 `operation`은 다시 대기열에 들어간 기존 Operation, `state.status`는 `QUEUED`.
- retry 전용 이벤트는 없다. 다시 실행되는 Step의 started·completed, Operation·Execution completed가
  나간다. `execution.started`, `execution.operation_started`는 다시 나가지 않는다.
- Operation 예산과 Step 시각은 새로 시작하지만 Execution 최초 시작·전체 만료 시각은 유지된다.

## 12. MULTI 다음 Operation — `POST /executions/{execution_id}/operations`

| 바디 | 필수 | 의미 |
| --- | :---: | --- |
| `idempotency_key` | O | Execution 단위 키 |
| `expected_version` | O | 현재 `state.version`. 0 이상 |
| `operation_timeout_seconds` | X | 새 Operation 예산. 1 이상 |
| `spec` | O | 제출과 같은 ExecutionSpec. sequence는 기존 Step 수부터 이어서 연속 |
| `metadata` | X | 이 Operation의 metadata |

```json
{
  "idempotency_key": "task-100-operation-2",
  "expected_version": 4,
  "operation_timeout_seconds": 600,
  "spec": {
    "schema_version": "1.0",
    "steps": [
      {
        "sequence": 2,
        "payload": {"type": "PYTHON_EXECUTE", "source": {"type": "INLINE", "content": "display(result.describe())"}},
        "step_timeout_seconds": 300
      }
    ]
  },
  "metadata": {"reason": "previous_result_followup"}
}
```

- 조건: `MULTI`, `WAITING_FOR_OPERATION`, `expected_version` 일치. 상태·버전을 먼저 보므로 오래된
  버전이면 Step 오류보다 `409`가 먼저 나온다.
- 제출과 달리 `lifecycle`, `trigger`, `runtime`, `operation` 래퍼를 보내지 않는다.
- 같은 커널을 이어 쓰므로 이전 Operation의 변수를 쓸 수 있다.
- 응답 `Location`은 새 Operation 상세. `state.status`는 `QUEUED`.
- 접수하면 대기 기한이 해제되고, Operation이 끝나면 다시 계산된다. 전체 만료 시각은 늘어나지 않는다.
- 같은 `expected_version`으로 동시에 두 요청이 오면 하나만 성공하고 나머지는 `409`다.

## 13. MULTI 종료 — `POST /executions/{execution_id}/finalize`

| 바디 | 필수 | 의미 |
| --- | :---: | --- |
| `idempotency_key` | O | Execution 단위 키 |
| `expected_version` | O | 현재 `state.version` |

- 조건: `MULTI`, `WAITING_FOR_OPERATION`, 버전 일치, **마지막 Operation의 Step이 모두 성공**.
  마지막 Operation이 실패했다면 보정 Operation을 성공시킨 뒤 finalize하거나 cancel한다.
- 응답 `state.status`는 `FINALIZING`, `operation`은 `null`. Worker가 노트북을 저장하고 커널을
  해제하면 `SUCCEEDED`(또는 `COMPLETION_FAILED`로 `FAILED`)가 되고 `execution.completed`가 나간다.
- 최종 리포트를 자동으로 만드는 API가 아니다.

## 14. Operation·Step·Attempt 조회

| API | 응답 주요 필드 |
| --- | --- |
| `GET .../operations` | `items[]`: `operation_id`, `operation_number`(1부터), `sequence_range{first,last}`, `operation_timeout_seconds`, `metadata`, `execution_attempt_id`, `result{status, error_message}`(`QUEUED`/`RUNNING`/`SUCCEEDED`/`FAILED`/`CANCELLED`), `lifecycle`, `step_count` |
| `GET .../operations/{operation_id}` | 위와 같은 Operation 하나 |
| `GET .../steps`, `.../operations/{id}/steps` | 현재 Step 요약 목록 |
| `GET .../steps/{step_id}` | `step_id`, `sequence`, `code_hash`, `source{type, path, sha256}`, `step_timeout_seconds`, `lineage`, `result{status, output_summary, error_message, result_ref}`, `lifecycle`. Step `status`: `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `SKIPPED`, `CANCELLED` |
| `GET .../attempts` | `items[]`: `attempt_id`, `attempt_number`, `state.status`(`RUNNING`/`WAITING`/`SUCCEEDED`/`FAILED`/`CANCELLED`), `failure`, `lifecycle`, `step_count` |
| `GET .../attempts/{attempt_id}` | 위 + `runtime{type, profile, target_id, kernel_id}`, `lease{owner, expires_at, heartbeat_at}`, `recovery{retry_strategy, runtime_session_cleanup_status, runtime_abort_status}` |
| `GET .../attempts/{attempt_id}/steps` | 그 Attempt에서 실행된 Step 결과(불변): `step_attempt_id`, `execution_step_id`, `sequence`, `tool`, `result`, `lifecycle` |

Attempt는 Worker가 실행을 맡을 때마다 하나씩 생긴다(최초 실행, retry, 장애 복구, MULTI의
각 Operation 실행). `lease.heartbeat_at`은 Worker 생존 신호이지 코드 진척이 아니다.
목록은 모두 `cursor`, `limit`(1~200, 기본 100)을 받는다.

## 15. 이벤트 이력 — `GET /executions/{execution_id}/events`

| 쿼리 | 의미 |
| --- | --- |
| `after_sequence` | 이 순번 다음부터. 0 이상 |
| `cursor`, `limit` | `limit` 1~500, 기본 200 |

`items[]`: `event_id`, `event_sequence`, `schema_version`, `event_type`, `payload`, `occurred_at`,
`delivery{status(PENDING/PUBLISHED), attempt_count, available_at, published_at, last_error}`.
Redis에서 순번 누락을 발견했을 때만 `after_sequence={마지막 순번}`으로 부른다. 오래된 이벤트는
전송 기록이 정리돼 `delivery`가 `null`이다. payload는 [Executor 이벤트](executor-events.md)와 같다.

## 16. 진단 — `GET /executions/{execution_id}/diagnostics`

쿼리 `attempt_id`, `operation_id`, `step_id`(필터), `cursor`, `limit`. 실패 원인 관찰 기록이다.
필드는 [진단 이력](execution-diagnostics.md).

## 17. Artifact

### 목록·상세

`GET .../artifacts`(`limit` 1~1000), `GET /artifacts/{artifact_id}`.

| 필드 | 의미 |
| --- | --- |
| `artifact_id`, `name`, `description` | 식별과 표시 이름 |
| `type` | `DATASET`, `NOTEBOOK`, `REPORT`, `PLOT`, `MODEL`, `METRIC`, `LOG`, `OTHER` |
| `status` | `AVAILABLE`, `INCOMPLETE`, `DELETED`. `AVAILABLE`은 파일이 있다는 뜻이지 실행 성공을 뜻하지 않는다 |
| `produced_by` | `execution_id`, `execution_attempt_id`, `execution_step_id`, `execution_step_attempt_id`. 노트북은 Step 값이 `null` |
| `lineage` | `parent_artifact_id`, `external_parent_asset_id` |
| `storage` | `type`(`PV`/`S3`), `uri`, `relative_path`(Jupyter PV 기준), `media_type`, `size_bytes`, `checksum_sha256`(등록 시점 관찰값) |
| `metadata` | Artifact metadata |

노트북 `notebooks/execution.ipynb`은 처음 저장될 때 `NOTEBOOK`/`AVAILABLE`로 등록되고 실행 중·실패·취소
후에도 받을 수 있다.

### 작성 — `POST /executions/{execution_id}/artifacts`

Agent가 쓴 UTF-8 텍스트를 Runtime 저장소에 Artifact로 만든다. 성공한 Execution에만 쓸 수 있다.

| 바디 | 필수 | 의미 |
| --- | :---: | --- |
| `idempotency_key` | O | Execution 단위 키 |
| `type` | O | `DATASET`, `MODEL`을 뺀 유형(보통 `REPORT`) |
| `source` | O | INLINE `{"type":"INLINE","content":"..."}` 또는 PATH `{"type":"PATH","path":"...","sha256":"..."}`(공유 루트 기준) |
| `name`, `description`, `media_type` | X | 표시 정보 |
| `append_to_notebook` | X | `true`면 노트북에도 Markdown 셀로 붙인다. `REPORT`만 가능 |
| `metadata` | X | Artifact metadata |

`REPORT`는 `reports/` 아래에 쓰이며 경로는 호출자가 고르지 않는다. 응답은 Artifact 상세,
`Location: /api/v1/artifacts/{artifact_id}`.

### 다운로드 — `GET /artifacts/{artifact_id}/content`

REST 전용. 현재 파일 전체(`200`) 또는 `Range: bytes=…` 한 구간(`206`)을 스트리밍한다.
응답 헤더 `ETag`, `X-Checksum-SHA256`은 지금 파일 기준이다. 자세한 규칙은
[Artifact 다운로드](get-artifact-content.md).

## 18. 노트북

| API | 쿼리 | 응답 |
| --- | --- | --- |
| `GET .../notebook` | `view`(`SUMMARY` 기본 / `FULL`), `start_index`(0부터), `limit`(1~200, 기본 20) | `execution_id`, `view`, `metadata`, `cells[]`, `page{start_index, limit, total_count, has_more}` |
| `GET .../notebook/cells/{cell_index}` | 없음 | `execution_id`, `cell`(항상 전체: `source`, `outputs` 포함) |

`SUMMARY`는 소스 미리보기와 출력 요약만, `FULL`은 소스 전체와 출력 전체를 준다. 노트북은 감사·편의용이며
Agent의 결과 경로는 9절의 결과 API다.

## 19. 관리자 API

모두 `admin` key가 필요하고, 쓰기에는 운영자 `X-User-Id`가 필요하다. tenant 헤더는 의미가 없다.

### Runtime 서버

| API | 바디 | 설명 |
| --- | --- | --- |
| `POST /runtime-targets` | `idempotency_key`, `name`(`^[a-zA-Z0-9._-]+$`), `runtime_type`(`JUPYTER`), `connection_config`(예: `{"endpoint": "http://jupyter-01:8888"}`), `credential`(토큰, 응답에 나오지 않음), `pool`(`INTERACTIVE`/`BATCH`), `max_concurrent_executions`(1~1000) | 등록 또는 수정 후 즉시 점검. 같은 endpoint를 두 이름으로 등록할 수 없다(`409 RUNTIME_TARGET_ENDPOINT_CONFLICT`) |
| `GET /runtime-targets` | 쿼리 `pool`, `runtime_type`, `status`, `enabled`, `cursor`, `limit` | 목록 |
| `GET /runtime-targets/{target_id}` | 없음 | 상세 |
| `POST /runtime-targets/{target_id}/probe` | 없음 | 즉시 점검 |
| `POST /runtime-targets/{target_id}/drain` | `idempotency_key` | 새 작업 배정 중지, 진행 중 작업은 계속 |
| `POST /runtime-targets/{target_id}/activate` | `idempotency_key` | 활성화·점검 후 배정 재개 |
| `POST /runtime-targets/{target_id}/disable` | `idempotency_key` | 비활성화(이력 보존) |
| `DELETE /runtime-targets/{target_id}` | 없음 | 등록 삭제(purge). DELETE라 멱등성 키가 없다 |
| `GET /runtime-pools` | 없음 | Pool별 `runtime{type, pool}`, `targets{total, enabled, active, draining, offline}`, `capacity{configured, schedulable, reserved_execution_count, available}`, `state{accepting_new_executions, saturated}` |

Runtime 서버 응답: `target_id`, `name`, `runtime{type, pool, connection_config, supported_profiles}`,
`state{status(ACTIVE/DRAINING/OFFLINE), enabled, accepting_new_executions, drain_complete}`,
`capacity{max_concurrent_executions, active_execution_count, active_session_count, admission_used_count,
available_capacity, admission_blocked, session_count_observed_at, session_count_fresh}`,
`health{last_check_at, last_error}`, `resources{cpu, memory, pressure_score, ...}`, 감사 필드.

#### purge

비활성화(`enabled=false`, `OFFLINE`)됐고, 미완료 작업·활성 Attempt·해제가 확인되지 않은 커널이
없을 때만 등록과 인증정보를 지운다. Execution·Attempt·이벤트·Artifact·파일과 과거
`runtime_target_id`는 보존한다.

| 상황 | HTTP |
| --- | --- |
| 삭제 조건 충족 | 200, 삭제 기록 `{target_id, name, 감사 필드}` |
| 이미 삭제된 UUID | 200, 최초 삭제 기록(반복 요청자로 덮어쓰지 않음) |
| 등록도 삭제 기록도 없음 | 404 `RUNTIME_TARGET_NOT_FOUND` |
| 활성·진행 중 작업·미확인 커널 | 409 `RUNTIME_TARGET_PURGE_CONFLICT` |

purge 후 같은 이름·endpoint로 다시 등록할 수 있지만 새 `idempotency_key`를 쓴다. 새 UUID가 생기며
과거 실행을 이어받지 않는다.

### Executor 유지보수

| API | 바디 | 설명 |
| --- | --- | --- |
| `GET /maintenance` | 없음 | `admission{state(ACTIVE/DRAINING), accepting_new_executions, version}`, `workload{queued_execution_count, active_execution_count, cancel_requested_count}`, `cleanup{unresolved_cleanup_count, active_runtime_session_count}`, `active_run`, `safe_to_shutdown` |
| `POST /maintenance/drain` | `idempotency_key` | 모든 Worker가 새 Execution 수용을 멈춘다 |
| `POST /maintenance/activate` | `idempotency_key` | 수용 재개 |
| `POST /maintenance/runs` | `idempotency_key`, `action`(`STOP_ACTIVE_EXECUTIONS`) | 진행 중 Execution을 일괄 중지하는 Run. `202` |
| `GET /maintenance/runs/{run_id}` | 없음 | `maintenance_run_id`, `action`, `status`(`REQUESTED`/`RUNNING`/`SUCCEEDED`/`FAILED`), `targets{total, pending, stop_requested, stopped, failed, remaining}`, `error_message`, `started_at`, `finished_at` |
| `GET /maintenance/runs/{run_id}/targets` | 쿼리 `cursor`, `limit` | Run이 잡은 Execution별 `execution_id`, `selected_execution_status`, `status`, `stop_requested_at`, `completed_at` |

운영 절차는 [유지보수 운영](../docs/executor-maintenance.md).

## 20. 상태 확인 엔드포인트

| 경로 | 인증 | 용도 |
| --- | --- | --- |
| `GET /healthz` | 없음 | 프로세스 생존 |
| `GET /readyz` | 없음 | DB 연결·스키마 버전·Worker 수용 여부 |
| `GET /workerz` | admin key | Worker 내부 상태 |
