# 실행(Execution) API 스펙

Agent·BFF·배치가 코드를 실행하고 결과를 읽는 API 25개다. 기준 경로 `/api/v1`. 공통 헤더·멱등성·버전·페이지·오류 규칙은 [README](README.md#공통-규칙)를 먼저 본다. 모든 엔드포인트는 공통 헤더(`X-Api-Key`, `X-Tenant-Id`, `X-User-Id`)를 받으며, 아래 파라미터 표에서는 생략한다. `X-Project-Id`, `X-Session-Id`는 제출(`POST /executions`)에서만 저장하므로 제출 표에만 적는다.


## 목록

| # | 메서드 · 경로 | MCP 도구 | 용도 |
| ---: | --- | --- | --- |
| 1 | `POST /api/v1/executions` | `execution_submit` | [Execution 제출](#ep-1) |
| 2 | `POST /api/v1/executions/{execution_id}/cancel` | `execution_cancel` | [취소 요청](#ep-2) |
| 3 | `POST /api/v1/executions/{execution_id}/retry` | `execution_retry` | [재시도](#ep-3) |
| 4 | `POST /api/v1/executions/{execution_id}/operations` | `execution_operation_create` | [MULTI 다음 Operation 추가](#ep-4) |
| 5 | `POST /api/v1/executions/{execution_id}/finalize` | `execution_finalize` | [MULTI 종료](#ep-5) |
| 6 | `GET /api/v1/executions` | `execution_list` | [Execution 목록](#ep-6) |
| 7 | `GET /api/v1/executions/{execution_id}` | `execution_get` | [Execution 상태](#ep-7) |
| 8 | `GET /api/v1/executions/{execution_id}/result` | `execution_result_get` | [통합 결과](#ep-8) |
| 9 | `GET /api/v1/executions/{execution_id}/notebook` | `execution_notebook_read` | [노트북 페이지](#ep-9) |
| 10 | `GET /api/v1/executions/{execution_id}/notebook/cells/{cell_index}` | `execution_notebook_cell_read` | [노트북 셀 하나](#ep-10) |
| 11 | `GET /api/v1/executions/{execution_id}/operations` | `execution_operation_list` | [Operation 목록](#ep-11) |
| 12 | `GET /api/v1/executions/{execution_id}/diagnostics` | 없음 | [실패 진단](#ep-12) |
| 13 | `GET /api/v1/executions/{execution_id}/steps` | `execution_step_list` | [Step 목록](#ep-13) |
| 14 | `GET /api/v1/executions/{execution_id}/steps/{step_id}` | 없음 | [Step 상세](#ep-14) |
| 15 | `GET /api/v1/executions/{execution_id}/operations/{operation_id}` | `execution_operation_get` | [Operation 상세](#ep-15) |
| 16 | `GET /api/v1/executions/{execution_id}/operations/{operation_id}/result` | `execution_operation_result_get` | [Operation 결과](#ep-16) |
| 17 | `GET /api/v1/executions/{execution_id}/operations/{operation_id}/steps` | `execution_operation_step_list` | [Operation의 Step 목록](#ep-17) |
| 18 | `GET /api/v1/executions/{execution_id}/attempts` | `execution_attempt_list` | [Attempt 목록](#ep-18) |
| 19 | `GET /api/v1/executions/{execution_id}/attempts/{attempt_id}` | `execution_attempt_get` | [Attempt 상세](#ep-19) |
| 20 | `GET /api/v1/executions/{execution_id}/attempts/{attempt_id}/steps` | `execution_attempt_step_list` | [Attempt의 Step 결과](#ep-20) |
| 21 | `GET /api/v1/executions/{execution_id}/events` | `execution_event_list` | [이벤트 이력](#ep-21) |
| 22 | `GET /api/v1/executions/{execution_id}/artifacts` | `execution_artifact_list` | [Artifact 목록](#ep-22) |
| 23 | `POST /api/v1/executions/{execution_id}/artifacts` | `execution_artifact_create` | [텍스트 Artifact 작성](#ep-23) |
| 24 | `GET /api/v1/artifacts/{artifact_id}` | `execution_artifact_get` | [Artifact 상세](#ep-24) |
| 25 | `GET /api/v1/artifacts/{artifact_id}/content` | 없음 | [Artifact 다운로드](#ep-25) |

## 실행 명령

상태를 바꾸는 API다. 모두 `X-User-Id`가 필요하고, 그 사용자가 Execution 소유자여야 한다(제출 제외). 응답 `202`는 요청이 저장됐다는 뜻이지 실행이 끝났다는 뜻이 아니다.

<a id="ep-1"></a>
### 1. Execution 제출

`POST /api/v1/executions`

- MCP: `execution_submit`
- 원문 요약: Submit an asynchronous execution

새 Execution과 첫 Operation을 만든다. 응답 헤더 `Location: /api/v1/executions/{execution_id}`.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| header | `X-Project-Id` |  | string \| null |  | 이 Execution이 속할 프로젝트. 형식 `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`, `unscoped` 금지. 제출 때만 저장 |
| header | `X-Session-Id` |  | string \| null |  | 이 Execution이 속할 호출자 세션. 형식은 `X-Project-Id`와 같고 `X-Project-Id`가 있어야 한다. 제출 때만 저장 |

**요청 바디** `ExecutionSubmitRequest` (모르는 필드는 거부)

| 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `lifecycle` | ExecutionLifecycleInput | O |  | 실행 방식 |
| `lifecycle.operation_mode` | `SINGLE` \| `MULTI` | O |  | `SINGLE`: 제출한 Operation이 끝나면 Execution 종료. `MULTI`: Operation이 끝나면 `WAITING_FOR_OPERATION`에서 다음 요청을 기다림 |
| `lifecycle.operation_wait_timeout_seconds` | integer \| null |  | ≥30 | MULTI 필수: 다음 Operation을 기다리는 최대 초(30 이상). SINGLE에서 보내면 `422` |
| `trigger` | ExecutionTriggerInput | O |  | 트리거 |
| `trigger.type` | `INTERACTIVE` \| `BATCH` | O |  | `INTERACTIVE` 또는 `BATCH`. Runtime Pool이 이 값으로 정해진다 |
| `runtime` | ExecutionRuntimeInput | O |  | Runtime 정보 |
| `runtime.type` | `JUPYTER` | O |  | Runtime 종류. 현재 `JUPYTER`만 |
| `runtime.profile` | string | O | 길이≥1, 길이≤128 | 커널 프로파일(1~128자). 기본 허용 `default`, `3102311`(배포 설정 `RUNTIME_ALLOWED_PROFILES`) |
| `operation` | ExecutionOperationInput | O |  | 첫 Operation |
| `operation.operation_timeout_seconds` | integer \| null |  | ≥1 | Operation이 RUNNING이 된 때부터 재는 시간 예산(초, 1 이상). 생략하면 제한 없음 |
| `operation.spec` | ExecutionSpec | O |  | ExecutionSpec(실행할 Step 묶음) |
| `operation.spec.schema_version` | `1.0` | O |  | `"1.0"` |
| `operation.spec.steps` | array<ExecutionStepInput> | O | 개수≥1 | 1개 이상. 제출은 sequence 0부터, 다음 Operation은 기존 Step 수부터 연속·오름차순. 한 Operation 최대 100개, Execution 누적 최대 1000개(기본 설정) |
| `operation.spec.steps[].sequence` | integer | O | ≥0 | Execution 전체 기준 Step 순번 |
| `operation.spec.steps[].payload` | PythonExecutePayload | O |  | 실행 내용. 현재 `PYTHON_EXECUTE`만 |
| `operation.spec.steps[].payload.type` | `PYTHON_EXECUTE` | O |  | `PYTHON_EXECUTE` |
| `operation.spec.steps[].payload.source` | InlineStepSource \| PathStepSource | O |  | 코드 위치. `INLINE` 또는 `PATH` |
| `operation.spec.steps[].payload.source⟨INLINE⟩.type` | `INLINE` | O |  | `INLINE` |
| `operation.spec.steps[].payload.source⟨INLINE⟩.content` | string | O | 길이≥1 | 실행할 Python 코드. 공백만으로는 안 된다 |
| `operation.spec.steps[].payload.source⟨PATH⟩.type` | `PATH` | O |  | `PATH` |
| `operation.spec.steps[].payload.source⟨PATH⟩.path` | string | O | 길이≥1, 길이≤4096 | `STORAGE_ROOT` 기준 상대경로의 UTF-8 `.py` 파일(절대경로·`..` 금지). 제출 전에 파일을 다 써 둔다 |
| `operation.spec.steps[].payload.source⟨PATH⟩.sha256` | string | O | `^[0-9a-fA-F]{64}$` | 파일 바이트의 SHA-256(64자리 16진수). 다르면 `422` |
| `operation.spec.steps[].step_timeout_seconds` | integer \| null |  | ≥1 | Step 실행·출력 수집·결과 저장 제한(초, 1 이상). 생략하면 제한 없음. Operation 잔여 예산과 작은 쪽이 적용된다 |
| `operation.spec.steps[].lineage` | StepLineage \| null |  |  | 추적 정보(선택) |
| `operation.spec.steps[].lineage.skill_name` | string \| null |  | 길이≤255 | 추적용 Skill 이름 |
| `operation.spec.steps[].lineage.tool_name` | string \| null |  | 길이≤255 | 추적용 Tool 이름 |
| `operation.spec.steps[].lineage.input_parameters` | object |  |  | Tool 입력값. 키 32개 이하, 4096 bytes 이하 |
| `operation.metadata` | object |  |  | 이 Operation에만 붙는 metadata. 규칙은 최상위 `metadata`와 같다 |
| `metadata` | object |  |  | 호출자 소유 metadata(batch_id 등). 그대로 저장되고 상세 조회·`execution.completed`로 돌려받는다. 키 32개 이하, 키 `^[a-z][a-z0-9_]{0,63}$`, 직렬화 4096 bytes 이하. 비밀값 금지 |

예:

```json
{
  "idempotency_key": "submit-task-100-1",
  "lifecycle": {
    "operation_mode": "MULTI",
    "operation_wait_timeout_seconds": 600
  },
  "trigger": {
    "type": "INTERACTIVE"
  },
  "runtime": {
    "type": "JUPYTER",
    "profile": "default"
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
              "type": "INLINE",
              "content": "print('hello')"
            }
          },
          "step_timeout_seconds": 300,
          "lineage": {
            "skill_name": "eda",
            "tool_name": "preview",
            "input_parameters": {}
          }
        }
      ]
    }
  },
  "metadata": {
    "task_id": "task-100"
  }
}
```

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 202 | [`ExecutionCommandResponse`](#schema-executioncommandresponse) | 접수됨(비동기 처리) |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- 제출 멱등성 키는 tenant 단위다. 같은 키·같은 바디면 처음 만든 Execution을 다시 돌려준다.
- `X-Project-Id`/`X-Session-Id`는 이 Execution이 속할 곳으로 저장된다. 없으면 workspace 경로의 그 자리에 예약값 `unscoped`가 쓰인다.
- `lifecycle.operation_mode=MULTI`면 `operation_wait_timeout_seconds`가 필수, SINGLE이면 보내지 않는다.
- Step·Operation 시간 제한이 Executor 전체 제한(`EXECUTION_MAX_RUNTIME_SECONDS`, 기본 7일)을 넘으면 `422 INVALID_EXECUTION_SPEC`.
- PATH source의 `sha256`은 `hashlib.sha256(Path(root, path).read_bytes()).hexdigest()`로 계산한다. 줄바꿈 하나만 달라도 값이 바뀐다.
- 주요 오류: `422 REQUEST_VALIDATION_ERROR`(형식), `422 INVALID_EXECUTION_SPEC`(규칙), `422 UNSUPPORTED_RUNTIME_PROFILE`, `409 IDEMPOTENCY_CONFLICT`, `413 REQUEST_BODY_TOO_LARGE`.

<a id="ep-2"></a>
### 2. 취소 요청

`POST /api/v1/executions/{execution_id}/cancel`

- MCP: `execution_cancel`
- 원문 요약: Request asynchronous cancellation

진행 중 Execution의 취소를 요청한다. Worker 확인을 기다리지 않고 바로 돌아온다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |

**요청 바디** `ExecutionCancelRequest` (모르는 필드는 거부)

| 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `reason` | string \| null |  | 길이≤2000 | 취소 사유(2000자 이하) |

예:

```json
{
  "idempotency_key": "cancel-1",
  "reason": "사용자가 분석을 취소했습니다."
}
```

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 202 | [`ExecutionCommandResponse`](#schema-executioncommandresponse) | 접수됨(비동기 처리) |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- terminal 상태(`CANCELLED`/`SUCCEEDED`/`FAILED`)에는 `409 INVALID_STATE_TRANSITION`. 이미 `CANCEL_REQUESTED`면 현재 상태를 그대로 돌려준다.
- 응답 `state.status`는 보통 `CANCEL_REQUESTED`, `operation`은 `null`.
- 이후 `execution.completed`(`CANCELLED` 또는 `FAILED`)가 나간다. 실행 중이던 Step은 `CANCELLED`, 시작 전 Step은 `SKIPPED`.
- `CANCELLED`는 커널 정리 성공을 뜻하지 않는다. `recovery.runtime_session_cleanup_status`를 본다.

<a id="ep-3"></a>
### 3. 재시도

`POST /api/v1/executions/{execution_id}/retry`

- MCP: `execution_retry`
- 원문 요약: Retry a failed execution

실패한 SINGLE Execution을 같은 Execution·Operation 안에서 다시 실행한다. 전략과 시작 sequence는 Executor가 정한다(`retry.strategy`, `retry.from_sequence`).

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |

**요청 바디** `ExecutionRetryRequest` (모르는 필드는 거부)

| 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |

예:

```json
{
  "idempotency_key": "retry-1"
}
```

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 202 | [`ExecutionCommandResponse`](#schema-executioncommandresponse) | 접수됨(비동기 처리) |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- 조건: `FAILED`, `SINGLE`, `retry.strategy != NOT_RETRYABLE`. 어기면 `409 INVALID_STATE_TRANSITION`.
- `FROM_FAILED_STEP`은 실패 커널이 보존 기한(`FAILED_SESSION_RETENTION_SECONDS`, 기본 1시간) 안에 살아 있어야 하고, `FROM_START`는 버려진 커널 정리가 끝나 있어야 한다.
- MULTI 실패는 retry 대신 고친 코드로 다음 Operation을 제출한다.
- 응답 `operation`은 다시 대기열에 들어간 기존 Operation, `state.status`는 `QUEUED`.
- retry 전용 이벤트는 없다. `execution.started`, `execution.operation_started`는 다시 나가지 않는다.

<a id="ep-4"></a>
### 4. MULTI 다음 Operation 추가

`POST /api/v1/executions/{execution_id}/operations`

- MCP: `execution_operation_create`
- 원문 요약: Append the next Operation to a MULTI execution

`WAITING_FOR_OPERATION`인 MULTI Execution에 다음 Step 묶음을 붙인다. 같은 커널을 이어 쓰므로 이전 Operation의 변수를 쓸 수 있다. 응답 `Location`은 새 Operation 상세.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |

**요청 바디** `ExecutionOperationCreateRequest` (모르는 필드는 거부)

| 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `expected_version` | integer | O | ≥0 | 호출자가 알고 있는 현재 `state.version`. 다르면 `409` |
| `operation_timeout_seconds` | integer \| null |  | ≥1 | Operation이 RUNNING이 된 때부터 재는 시간 예산(초, 1 이상). 생략하면 제한 없음 |
| `spec` | ExecutionSpec | O |  | ExecutionSpec(실행할 Step 묶음) |
| `spec.schema_version` | `1.0` | O |  | `"1.0"` |
| `spec.steps` | array<ExecutionStepInput> | O | 개수≥1 | 1개 이상. 제출은 sequence 0부터, 다음 Operation은 기존 Step 수부터 연속·오름차순. 한 Operation 최대 100개, Execution 누적 최대 1000개(기본 설정) |
| `spec.steps[].sequence` | integer | O | ≥0 | Execution 전체 기준 Step 순번 |
| `spec.steps[].payload` | PythonExecutePayload | O |  | 실행 내용. 현재 `PYTHON_EXECUTE`만 |
| `spec.steps[].payload.type` | `PYTHON_EXECUTE` | O |  | `PYTHON_EXECUTE` |
| `spec.steps[].payload.source` | InlineStepSource \| PathStepSource | O |  | 코드 위치. `INLINE` 또는 `PATH` |
| `spec.steps[].payload.source⟨INLINE⟩.type` | `INLINE` | O |  | `INLINE` |
| `spec.steps[].payload.source⟨INLINE⟩.content` | string | O | 길이≥1 | 실행할 Python 코드. 공백만으로는 안 된다 |
| `spec.steps[].payload.source⟨PATH⟩.type` | `PATH` | O |  | `PATH` |
| `spec.steps[].payload.source⟨PATH⟩.path` | string | O | 길이≥1, 길이≤4096 | `STORAGE_ROOT` 기준 상대경로의 UTF-8 `.py` 파일(절대경로·`..` 금지). 제출 전에 파일을 다 써 둔다 |
| `spec.steps[].payload.source⟨PATH⟩.sha256` | string | O | `^[0-9a-fA-F]{64}$` | 파일 바이트의 SHA-256(64자리 16진수). 다르면 `422` |
| `spec.steps[].step_timeout_seconds` | integer \| null |  | ≥1 | Step 실행·출력 수집·결과 저장 제한(초, 1 이상). 생략하면 제한 없음. Operation 잔여 예산과 작은 쪽이 적용된다 |
| `spec.steps[].lineage` | StepLineage \| null |  |  | 추적 정보(선택) |
| `spec.steps[].lineage.skill_name` | string \| null |  | 길이≤255 | 추적용 Skill 이름 |
| `spec.steps[].lineage.tool_name` | string \| null |  | 길이≤255 | 추적용 Tool 이름 |
| `spec.steps[].lineage.input_parameters` | object |  |  | Tool 입력값. 키 32개 이하, 4096 bytes 이하 |
| `metadata` | object |  |  | 새 Operation의 metadata. 규칙은 제출 `metadata`와 같다 |

예:

```json
{
  "idempotency_key": "task-100-operation-2",
  "expected_version": 4,
  "operation_timeout_seconds": 600,
  "spec": {
    "schema_version": "1.0",
    "steps": [
      {
        "sequence": 1,
        "payload": {
          "type": "PYTHON_EXECUTE",
          "source": {
            "type": "INLINE",
            "content": "display(df.describe())"
          }
        }
      }
    ]
  }
}
```

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 202 | [`ExecutionCommandResponse`](#schema-executioncommandresponse) | 접수됨(비동기 처리) |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- 조건: `MULTI`, `WAITING_FOR_OPERATION`, `expected_version` 일치. 상태·버전을 먼저 보므로 오래된 버전이면 Step 오류보다 `409`가 먼저 나온다.
- `spec.steps[].sequence`는 기존 Step 수부터 이어서 연속이어야 한다.
- 제출과 달리 `lifecycle`, `trigger`, `runtime`, `operation` 래퍼를 보내지 않는다.
- 접수하면 대기 기한이 해제되고 Operation이 끝나면 다시 계산된다. 전체 만료 시각은 늘어나지 않는다.
- 같은 `expected_version`으로 동시에 두 요청이 오면 하나만 성공한다.

<a id="ep-5"></a>
### 5. MULTI 종료

`POST /api/v1/executions/{execution_id}/finalize`

- MCP: `execution_finalize`
- 원문 요약: Finalize a waiting MULTI execution

대기 중인 MULTI Execution을 끝내고 보존 커널을 해제한다. 응답 `state.status`는 `FINALIZING`, Worker가 노트북 저장·커널 해제를 마치면 `SUCCEEDED`(또는 `COMPLETION_FAILED`로 `FAILED`)와 `execution.completed`가 나간다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |

**요청 바디** `ExecutionFinalizeRequest` (모르는 필드는 거부)

| 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `expected_version` | integer | O | ≥0 | 호출자가 알고 있는 현재 `state.version`. 다르면 `409` |

예:

```json
{
  "idempotency_key": "finalize-1",
  "expected_version": 7
}
```

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 202 | [`ExecutionCommandResponse`](#schema-executioncommandresponse) | 접수됨(비동기 처리) |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- 조건: `MULTI`, `WAITING_FOR_OPERATION`, 버전 일치, **마지막 Operation의 Step이 모두 성공**. 마지막 Operation이 실패했다면 보정 Operation을 성공시킨 뒤 finalize하거나 cancel한다.
- 최종 리포트를 자동으로 만드는 API가 아니다.

## 실행 조회

PostgreSQL 원본 상태를 읽는다. 이벤트 누락·재시작 뒤 상태 확인, 결과 읽기에 쓴다.

<a id="ep-6"></a>
### 6. Execution 목록

`GET /api/v1/executions`

- MCP: `execution_list`
- 원문 요약: List execution history

호출자 tenant의 Execution을 최신순으로 나열한다. `X-User-Id`를 보내면 그 사용자의 것만 나온다. `X-User-Id` 없이 부르는 시스템 호출자는 쿼리 `user_id`로 소유자를 거를 수 있다. 필터는 AND이며 시간 필터는 없다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| query | `user_id` |  | string \| null | `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` | 소유자로 거르기(소문자로 비교). `X-User-Id`와 함께 보내 값이 다르면 `403 FORBIDDEN` |
| query | `project_id` |  | string \| null | `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$` | 프로젝트로 거르기 |
| query | `session_id` |  | string \| null | `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$` | 세션으로 거르기 |
| query | `trigger_type` |  | `INTERACTIVE` \| `BATCH` \| null |  | 트리거로 거르기 |
| query | `status` |  | `QUEUED` \| `RUNNING` \| `WAITING_FOR_OPERATION` \| `FINALIZING` \| `CANCEL_REQUESTED` \| `CANCELLED` \| `SUCCEEDED` \| `FAILED` \| null |  | 상태로 거르기 |
| query | `cursor` |  | string \| null | 길이≤2048 | 이전 응답의 `next_cursor`를 그대로 |
| query | `limit` |  | integer | ≥1, ≤200, 기본 `100` | 페이지 크기 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionPageResponse`](#schema-executionpageresponse) | 성공 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- 사용자·프로젝트·세션으로 거를 때는 헤더가 아니라 쿼리 `user_id`, `project_id`, `session_id`를 쓴다. 헤더 `X-User-Id`는 보이는 범위를 그 사용자로 제한한다.
- `user_id`와 `X-User-Id`를 함께 보내 값이 다르면(대소문자 무시) `403 FORBIDDEN`.
- 커서를 고치거나 다음 페이지에서 필터를 바꾸면 `422 INVALID_CURSOR`.

<a id="ep-7"></a>
### 7. Execution 상태

`GET /api/v1/executions/{execution_id}`

- MCP: `execution_get`
- 원문 요약: Get current execution state

Execution의 현재 상태 원본이다. 이벤트 누락, 재시작, 명령 반영 확인, `expected_version` 얻기에 쓴다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionResponse`](#schema-executionresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- `CANCELLED`여도 커널 정리가 `FAILED`일 수 있다. terminal 상태만으로 정리 여부를 추론하지 않는다.
- `failure.type=COMPLETION_FAILED`는 필수 결과 전달·최종 정리 실패이며 `NOT_RETRYABLE`이다. 이미 성공한 Step 결과는 그대로다.

<a id="ep-8"></a>
### 8. 통합 결과

`GET /api/v1/executions/{execution_id}/result`

- MCP: `execution_result_get`
- 원문 요약: Get the consolidated execution result for Agent reporting

이벤트를 받은 뒤 결과를 읽는 경로다. Execution 상태·버전, Operation별 현재 Step 결과, Attempt 요약, Artifact 요약을 한 번에 준다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionResultResponse`](#schema-executionresultresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- Step 원문 출력은 응답에 없다. `result_ref.relative_path`의 manifest를 공유 루트에 결합해 읽고 `checksum_sha256`으로 검증한다.
- Operation 하나만 볼 때는 `GET .../operations/{operation_id}/result`.

## 노트북

Runtime이 저장한 실행 노트북(`notebooks/execution.ipynb`)을 읽는다. 감사·편의용이며 결과의 원본은 결과 API다.

<a id="ep-9"></a>
### 9. 노트북 페이지

`GET /api/v1/executions/{execution_id}/notebook`

- MCP: `execution_notebook_read`
- 원문 요약: Read Runtime-owned execution notebook cells

노트북 셀을 index 범위로 읽는다. `SUMMARY`(기본)는 소스 미리보기와 출력 요약만, `FULL`은 소스와 출력 전체를 준다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |
| query | `view` |  | `SUMMARY` \| `FULL` | 기본 `"SUMMARY"` | `SUMMARY`(미리보기) 또는 `FULL`(전체) |
| query | `start_index` |  | integer | ≥0, 기본 `0` | 시작 셀 index |
| query | `limit` |  | integer | ≥1, ≤200, 기본 `20` | 페이지 크기 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionNotebookResponse`](#schema-executionnotebookresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- 노트북이 아직 없으면 `409 EXECUTION_NOTEBOOK_NOT_AVAILABLE`, 파일을 읽을 수 없으면 `422 NOTEBOOK_READ_ERROR`.

<a id="ep-10"></a>
### 10. 노트북 셀 하나

`GET /api/v1/executions/{execution_id}/notebook/cells/{cell_index}`

- MCP: `execution_notebook_cell_read`
- 원문 요약: Read one Runtime-owned execution notebook cell

셀 하나를 소스·출력 전체로 읽는다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |
| path | `cell_index` | O | integer | ≥0 | 셀 index(0부터) |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionNotebookCellResponse`](#schema-executionnotebookcellresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- 없는 index는 `404 NOTEBOOK_CELL_NOT_FOUND`. MCP 도구는 이미지 출력을 이미지 콘텐츠로 돌려준다.

## 이력(Operation·Step·Attempt·이벤트·진단)

실행의 세부 이력이다. 목록은 모두 `cursor`/`limit` 페이지를 쓴다.

<a id="ep-11"></a>
### 11. Operation 목록

`GET /api/v1/executions/{execution_id}/operations`

- MCP: `execution_operation_list`
- 원문 요약: List Agent-submitted execution Operations

Agent가 제출한 Operation(연속된 Step 한 묶음)을 순서대로 나열한다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |
| query | `cursor` |  | string \| null | 길이≤2048 | 이전 응답의 `next_cursor`를 그대로 |
| query | `limit` |  | integer | ≥1, ≤200, 기본 `100` | 페이지 크기 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionOperationPageResponse`](#schema-executionoperationpageresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-12"></a>
### 12. 실패 진단

`GET /api/v1/executions/{execution_id}/diagnostics`

- MCP: 없음(REST 전용)
- 원문 요약: List failure observations, including secondary delivery errors

실패 원인 관찰 기록이다. 부수 전달 오류(노트북 반영 실패 등)도 남는다. `attempt_id`, `operation_id`, `step_id`로 거를 수 있다. MCP 도구는 없다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |
| query | `attempt_id` |  | string(uuid) \| null |  | Attempt ID |
| query | `operation_id` |  | string(uuid) \| null |  | Operation ID |
| query | `step_id` |  | string(uuid) \| null |  | Step ID |
| query | `cursor` |  | string \| null | 길이≤2048 | 이전 응답의 `next_cursor`를 그대로 |
| query | `limit` |  | integer | ≥1, ≤200, 기본 `100` | 페이지 크기 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionDiagnosticPageResponse`](#schema-executiondiagnosticpageresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-13"></a>
### 13. Step 목록

`GET /api/v1/executions/{execution_id}/steps`

- MCP: `execution_step_list`
- 원문 요약: List current execution Steps

Execution의 현재 Step 요약을 sequence 순으로 나열한다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |
| query | `cursor` |  | string \| null | 길이≤2048 | 이전 응답의 `next_cursor`를 그대로 |
| query | `limit` |  | integer | ≥1, ≤200, 기본 `100` | 페이지 크기 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionStepPageResponse`](#schema-executionsteppageresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-14"></a>
### 14. Step 상세

`GET /api/v1/executions/{execution_id}/steps/{step_id}`

- MCP: 없음(REST 전용)
- 원문 요약: Get one current execution Step

Step 하나의 상세(코드 출처, 시간 제한, 결과 참조 포함)다. MCP 도구는 없다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |
| path | `step_id` | O | string(uuid) |  | Step ID |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionStepResponse`](#schema-executionstepresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-15"></a>
### 15. Operation 상세

`GET /api/v1/executions/{execution_id}/operations/{operation_id}`

- MCP: `execution_operation_get`
- 원문 요약: Get one execution Operation detail

Operation 하나의 상세다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |
| path | `operation_id` | O | string(uuid) |  | Operation ID |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionOperationResponse`](#schema-executionoperationresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- 다른 Execution의 Operation이면 `404 EXECUTION_OPERATION_NOT_FOUND`.

<a id="ep-16"></a>
### 16. Operation 결과

`GET /api/v1/executions/{execution_id}/operations/{operation_id}/result`

- MCP: `execution_operation_result_get`
- 원문 요약: Get one Operation and all of its Step results

`execution.operation_completed`를 받은 뒤 그 Operation과 Step 결과 전체를 읽는다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |
| path | `operation_id` | O | string(uuid) |  | Operation ID |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionOperationResultResponse`](#schema-executionoperationresultresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-17"></a>
### 17. Operation의 Step 목록

`GET /api/v1/executions/{execution_id}/operations/{operation_id}/steps`

- MCP: `execution_operation_step_list`
- 원문 요약: List current Step results for one Operation

Operation 하나에 속한 현재 Step 결과를 나열한다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |
| path | `operation_id` | O | string(uuid) |  | Operation ID |
| query | `cursor` |  | string \| null | 길이≤2048 | 이전 응답의 `next_cursor`를 그대로 |
| query | `limit` |  | integer | ≥1, ≤200, 기본 `100` | 페이지 크기 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionStepPageResponse`](#schema-executionsteppageresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-18"></a>
### 18. Attempt 목록

`GET /api/v1/executions/{execution_id}/attempts`

- MCP: `execution_attempt_list`
- 원문 요약: List immutable execution Attempt summaries

Attempt는 Worker가 실행을 맡을 때마다 하나씩 생긴다(최초 실행, retry, 장애 복구, MULTI의 각 Operation 실행). 한 번 끝난 Attempt는 바뀌지 않는다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |
| query | `cursor` |  | string \| null | 길이≤2048 | 이전 응답의 `next_cursor`를 그대로 |
| query | `limit` |  | integer | ≥1, ≤200, 기본 `100` | 페이지 크기 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionAttemptPageResponse`](#schema-executionattemptpageresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-19"></a>
### 19. Attempt 상세

`GET /api/v1/executions/{execution_id}/attempts/{attempt_id}`

- MCP: `execution_attempt_get`
- 원문 요약: Get one immutable execution Attempt

Attempt 하나와 그 Runtime·lease·복구 정보다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |
| path | `attempt_id` | O | string(uuid) |  | Attempt ID |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionAttemptDetailResponse`](#schema-executionattemptdetailresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- `lease.heartbeat_at`은 Worker 생존 신호이지 코드 진척이 아니다.

<a id="ep-20"></a>
### 20. Attempt의 Step 결과

`GET /api/v1/executions/{execution_id}/attempts/{attempt_id}/steps`

- MCP: `execution_attempt_step_list`
- 원문 요약: List immutable Step results for one Attempt

그 Attempt에서 실행된 Step 결과(불변)를 나열한다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |
| path | `attempt_id` | O | string(uuid) |  | Attempt ID |
| query | `cursor` |  | string \| null | 길이≤2048 | 이전 응답의 `next_cursor`를 그대로 |
| query | `limit` |  | integer | ≥1, ≤200, 기본 `100` | 페이지 크기 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionStepAttemptPageResponse`](#schema-executionstepattemptpageresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-21"></a>
### 21. 이벤트 이력

`GET /api/v1/executions/{execution_id}/events`

- MCP: `execution_event_list`
- 원문 요약: List the durable Execution event timeline

PostgreSQL에 저장된 이벤트 타임라인과 Redis 전송 상태다. Redis에서 순번 누락을 발견했을 때 `after_sequence={마지막으로 처리한 순번}`으로 부른다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |
| query | `after_sequence` |  | integer | ≥0, 기본 `0` | 이 순번 다음 이벤트부터 |
| query | `cursor` |  | string \| null | 길이≤2048 | 이전 응답의 `next_cursor`를 그대로 |
| query | `limit` |  | integer | ≥1, ≤500, 기본 `200` | 페이지 크기 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionEventPageResponse`](#schema-executioneventpageresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- payload는 [이벤트 스펙](05-events.md)과 같다. 정상적으로 연속된 이벤트에는 부르지 않는다.

## Artifact

실행이 만든 파일(노트북, 리포트, 그림 등)을 조회·작성·다운로드한다.

<a id="ep-22"></a>
### 22. Artifact 목록

`GET /api/v1/executions/{execution_id}/artifacts`

- MCP: `execution_artifact_list`
- 원문 요약: List execution Artifacts

Execution이 만든 Artifact를 나열한다. 노트북은 처음 저장될 때 `NOTEBOOK`/`AVAILABLE`로 등록되고, 실행 중·실패·취소 후에도 받을 수 있다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |
| query | `cursor` |  | string \| null | 길이≤2048 | 이전 응답의 `next_cursor`를 그대로 |
| query | `limit` |  | integer | ≥1, ≤1000, 기본 `100` | 페이지 크기 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionArtifactPageResponse`](#schema-executionartifactpageresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-23"></a>
### 23. 텍스트 Artifact 작성

`POST /api/v1/executions/{execution_id}/artifacts`

- MCP: `execution_artifact_create`
- 원문 요약: Materialize Agent-authored text as a Runtime-owned Artifact

Agent가 쓴 UTF-8 텍스트(예: 최종 리포트)를 Runtime 저장소에 Artifact로 만든다. 응답 `201`, `Location: /api/v1/artifacts/{artifact_id}`.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `execution_id` | O | string(uuid) |  | Execution ID |

**요청 바디** `ExecutionArtifactMaterializeRequest` (모르는 필드는 거부)

| 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `type` | `NOTEBOOK` \| `REPORT` \| `PLOT` \| `METRIC` \| `LOG` \| `OTHER` | O |  | Artifact 유형. `DATASET`, `MODEL`은 받지 않는다(보통 `REPORT`) |
| `source` | InlineArtifactSource \| PathArtifactSource | O |  | `INLINE` 텍스트 또는 `PATH` 파일 |
| `source⟨INLINE⟩.type` | `INLINE` | O |  | `INLINE` |
| `source⟨INLINE⟩.content` | string | O | 길이≥1 | Artifact로 저장할 UTF-8 텍스트 |
| `source⟨PATH⟩.type` | `PATH` | O |  | `PATH` |
| `source⟨PATH⟩.path` | string | O | 길이≥1, 길이≤4096 | 공유 루트 기준 상대경로의 UTF-8 텍스트 파일 |
| `source⟨PATH⟩.sha256` | string | O | `^[0-9a-fA-F]{64}$` | 파일 바이트의 SHA-256 |
| `name` | string \| null |  | 길이≥1, 길이≤255 | 표시 이름 |
| `description` | string \| null |  | 길이≤4000 | 설명 |
| `media_type` | string \| null |  | 길이≥1, 길이≤255 | MIME 유형 |
| `append_to_notebook` | boolean |  | 기본 `false` | `true`면 노트북 끝에 Markdown 셀로도 붙인다. `REPORT`만 가능 |
| `metadata` | object |  |  | Artifact metadata |

예:

```json
{
  "idempotency_key": "report-1",
  "type": "REPORT",
  "name": "final-report.md",
  "media_type": "text/markdown",
  "source": {
    "type": "INLINE",
    "content": "# 분석 결과\n..."
  },
  "append_to_notebook": true
}
```

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 201 | [`ExecutionArtifactResponse`](#schema-executionartifactresponse) | 생성됨 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- **`SUCCEEDED` Execution에만** 쓸 수 있다.
- `REPORT`는 `reports/` 아래에 쓰이며 경로는 호출자가 고르지 않는다.
- `append_to_notebook=true`는 `REPORT`만 가능하다.

<a id="ep-24"></a>
### 24. Artifact 상세

`GET /api/v1/artifacts/{artifact_id}`

- MCP: `execution_artifact_get`
- 원문 요약: Get one execution Artifact and lineage references

Artifact 하나와 lineage 참조다. 보이는 범위는 그 Artifact의 Execution 기준이다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `artifact_id` | O | string(uuid) |  | Artifact ID |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutionArtifactResponse`](#schema-executionartifactresponse) | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-25"></a>
### 25. Artifact 다운로드

`GET /api/v1/artifacts/{artifact_id}/content`

- MCP: 없음(REST 전용)
- 원문 요약: Stream registered Artifact content

현재 파일 전체(`200`) 또는 `Range: bytes=…` 한 구간(`206`)을 스트리밍한다. REST 전용(MCP 도구 없음).

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `artifact_id` | O | string(uuid) |  | Artifact ID |
| header | `Range` |  | string \| null |  | `bytes=시작-끝` 한 구간 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | - | 성공 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |
| 206 | - | 부분 내용(Range) |
| 416 | - | Range를 만족할 수 없음 |

**주의**

- 응답 헤더 `ETag`, `X-Checksum-SHA256`은 지금 파일 기준이다.
- 범위가 맞지 않으면 `416 ARTIFACT_RANGE_NOT_SATISFIABLE`, 파일이 없거나 읽을 수 없으면 `409 ARTIFACT_CONTENT_UNAVAILABLE`.

## 응답 스키마

응답에 나오는 모든 객체다. 타입 링크를 따라가면 하위 객체로 간다. `null` 가능 필드도 키는 항상 있다(필수 O).

<a id="schema-executioncommandresponse"></a>
#### `ExecutionCommandResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `execution_id` | string(uuid) | O | Execution ID |
| `operation` | [ExecutionOperationReceipt](#schema-executionoperationreceipt) \| null | O | 이 명령으로 생겼거나 다시 대기열에 들어간 Operation. cancel·finalize는 `null` |
| `state` | [ExecutionCommandState](#schema-executioncommandstate) | O | 상태 |

<a id="schema-errorresponse"></a>
#### `ErrorResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `error` | [ErrorDetail](#schema-errordetail) | O | 오류 |

<a id="schema-executionpageresponse"></a>
#### `ExecutionPageResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `next_cursor` | string \| null | O | 다음 페이지 커서. 그대로 `cursor`로 보낸다. 마지막 페이지면 `null` |
| `has_more` | boolean | O | 다음 페이지가 있는지 |
| `items` | array<[ExecutionSummaryResponse](#schema-executionsummaryresponse)> | O | 이번 페이지 항목 |

<a id="schema-executionresponse"></a>
#### `ExecutionResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `execution_id` | string(uuid) | O | Execution ID |
| `trigger_type` | `INTERACTIVE` \| `BATCH` | O | `INTERACTIVE` 또는 `BATCH`(`trigger.type`과 같은 값) |
| `trigger` | [ExecutionTrigger](#schema-executiontrigger) | O | 트리거 |
| `tenant_id` | string | O | Execution의 tenant. 이벤트 Stream 이름을 정한다. 바뀌지 않는다 |
| `context` | [ExecutionContext](#schema-executioncontext) | O | 소유 범위 |
| `metadata` | object | O | 제출 최상위 metadata. 키 이름에 token/secret/password/credential이 들어간 값은 `[REDACTED]` |
| `runtime` | [ExecutionRuntime](#schema-executionruntime) | O | Runtime 정보 |
| `state` | [ExecutionState](#schema-executionstate) | O | 상태 |
| `workspace` | [WorkspaceResponse](#schema-workspaceresponse) | O | Runtime workspace |
| `failure` | [FailureResponse](#schema-failureresponse) \| null | O | 실패 정보. 실패가 아니면 `null` |
| `retry` | [RetryResponse](#schema-retryresponse) | O | 재시도 정보 |
| `recovery` | [RecoveryResponse](#schema-recoveryresponse) | O | 복구·정리 상태 |
| `deadlines` | [DeadlinesResponse](#schema-deadlinesresponse) | O | 기한 |
| `lifecycle` | [ExecutionLifecycleResponse](#schema-executionlifecycleresponse) | O | 수명 시각 |

<a id="schema-executionresultresponse"></a>
#### `ExecutionResultResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `execution` | [ExecutionResultExecutionResponse](#schema-executionresultexecutionresponse) | O | Execution ID와 상태·버전 |
| `operations` | array<[ExecutionResultOperationResponse](#schema-executionresultoperationresponse)> | O | Operation별 결과 |
| `attempts` | array<[ExecutionAttemptResponse](#schema-executionattemptresponse)> | O | Attempt 요약 |
| `artifacts` | array<[ExecutionArtifactSummaryResponse](#schema-executionartifactsummaryresponse)> | O | Artifact 요약 |

<a id="schema-executionnotebookresponse"></a>
#### `ExecutionNotebookResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `execution_id` | string(uuid) | O | Execution ID |
| `view` | `SUMMARY` \| `FULL` | O | `SUMMARY` 또는 `FULL` |
| `metadata` | object | O | 노트북 metadata |
| `cells` | array<[NotebookCellSummaryResponse](#schema-notebookcellsummaryresponse) \| [NotebookCellResponse](#schema-notebookcellresponse)> | O | 셀 목록 |
| `page` | [NotebookPage](#schema-notebookpage) | O | 페이지 정보 |

<a id="schema-executionnotebookcellresponse"></a>
#### `ExecutionNotebookCellResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `execution_id` | string(uuid) | O | Execution ID |
| `cell` | [NotebookCellResponse](#schema-notebookcellresponse) | O | 셀(항상 전체 내용) |

<a id="schema-executionoperationpageresponse"></a>
#### `ExecutionOperationPageResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `next_cursor` | string \| null | O | 다음 페이지 커서. 그대로 `cursor`로 보낸다. 마지막 페이지면 `null` |
| `has_more` | boolean | O | 다음 페이지가 있는지 |
| `items` | array<[ExecutionOperationSummaryResponse](#schema-executionoperationsummaryresponse)> | O | 이번 페이지 항목 |

<a id="schema-executiondiagnosticpageresponse"></a>
#### `ExecutionDiagnosticPageResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `next_cursor` | string \| null | O | 다음 페이지 커서. 그대로 `cursor`로 보낸다. 마지막 페이지면 `null` |
| `has_more` | boolean | O | 다음 페이지가 있는지 |
| `items` | array<[ExecutionDiagnosticResponse](#schema-executiondiagnosticresponse)> | O | 이번 페이지 항목 |

<a id="schema-executionsteppageresponse"></a>
#### `ExecutionStepPageResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `next_cursor` | string \| null | O | 다음 페이지 커서. 그대로 `cursor`로 보낸다. 마지막 페이지면 `null` |
| `has_more` | boolean | O | 다음 페이지가 있는지 |
| `items` | array<[ExecutionStepSummaryResponse](#schema-executionstepsummaryresponse)> | O | 이번 페이지 항목 |

<a id="schema-executionstepresponse"></a>
#### `ExecutionStepResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `step_id` | string(uuid) | O | Step ID(Executor 발급) |
| `execution_id` | string(uuid) | O | Execution ID |
| `sequence` | integer | O | Step 순번 |
| `code_hash` | string \| null | O | 실행 코드의 해시 |
| `source` | [StepSourceResponse](#schema-stepsourceresponse) | O | 코드 출처 |
| `step_timeout_seconds` | integer \| null | O | Step 실행·출력 수집·결과 저장 제한(초, 1 이상). 생략하면 제한 없음. Operation 잔여 예산과 작은 쪽이 적용된다 |
| `lineage` | [ToolReference](#schema-toolreference) | O | 추적 정보 |
| `result` | [StepResult](#schema-stepresult) | O | 결과 |
| `lifecycle` | [Lifecycle](#schema-lifecycle) | O | 수명 시각 |

<a id="schema-executionoperationresponse"></a>
#### `ExecutionOperationResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `operation_id` | string(uuid) | O | Operation ID |
| `execution_id` | string(uuid) | O | Execution ID |
| `operation_number` | integer | O | Execution 안의 Operation 순번(1부터) |
| `schema_version` | string | O | 스키마 버전(`1.0`) |
| `sequence_range` | [OperationSequenceRange](#schema-operationsequencerange) | O | 이 Operation이 담은 Step sequence 범위 |
| `operation_timeout_seconds` | integer \| null | O | Operation이 RUNNING이 된 때부터 재는 시간 예산(초, 1 이상). 생략하면 제한 없음 |
| `metadata` | object | O | Operation metadata |
| `execution_attempt_id` | string(uuid) \| null | O | 이 Operation을 마지막으로 실행한 Attempt |
| `result` | [OperationResult](#schema-operationresult) | O | 결과 |
| `lifecycle` | [Lifecycle](#schema-lifecycle) | O | 수명 시각 |
| `step_count` | integer | O | Step 수 |

<a id="schema-executionoperationresultresponse"></a>
#### `ExecutionOperationResultResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `execution` | [ExecutionResultExecutionResponse](#schema-executionresultexecutionresponse) | O | Execution ID와 상태·버전 |
| `operation` | [ExecutionResultOperationResponse](#schema-executionresultoperationresponse) | O | Operation과 그 Step 결과 |

<a id="schema-executionattemptpageresponse"></a>
#### `ExecutionAttemptPageResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `next_cursor` | string \| null | O | 다음 페이지 커서. 그대로 `cursor`로 보낸다. 마지막 페이지면 `null` |
| `has_more` | boolean | O | 다음 페이지가 있는지 |
| `items` | array<[ExecutionAttemptResponse](#schema-executionattemptresponse)> | O | 이번 페이지 항목 |

<a id="schema-executionattemptdetailresponse"></a>
#### `ExecutionAttemptDetailResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `attempt_id` | string(uuid) | O | Attempt ID |
| `execution_id` | string(uuid) | O | Execution ID |
| `attempt_number` | integer | O | Attempt 순번(1부터) |
| `state` | [AttemptState](#schema-attemptstate) | O | 상태 |
| `failure` | [FailureResponse](#schema-failureresponse) \| null | O | 실패 정보. 실패가 아니면 `null` |
| `lifecycle` | [Lifecycle](#schema-lifecycle) | O | 수명 시각 |
| `step_count` | integer | O | Step 수 |
| `runtime` | [AttemptRuntime](#schema-attemptruntime) | O | Runtime 정보 |
| `lease` | [AttemptLease](#schema-attemptlease) | O | Worker 소유권 |
| `recovery` | [AttemptRecovery](#schema-attemptrecovery) | O | 복구·정리 상태 |

<a id="schema-executionstepattemptpageresponse"></a>
#### `ExecutionStepAttemptPageResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `next_cursor` | string \| null | O | 다음 페이지 커서. 그대로 `cursor`로 보낸다. 마지막 페이지면 `null` |
| `has_more` | boolean | O | 다음 페이지가 있는지 |
| `items` | array<[ExecutionStepAttemptSummaryResponse](#schema-executionstepattemptsummaryresponse)> | O | 이번 페이지 항목 |

<a id="schema-executioneventpageresponse"></a>
#### `ExecutionEventPageResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `next_cursor` | string \| null | O | 다음 페이지 커서. 그대로 `cursor`로 보낸다. 마지막 페이지면 `null` |
| `has_more` | boolean | O | 다음 페이지가 있는지 |
| `items` | array<[ExecutionEventResponse](#schema-executioneventresponse)> | O | 이번 페이지 항목 |

<a id="schema-executionartifactpageresponse"></a>
#### `ExecutionArtifactPageResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `next_cursor` | string \| null | O | 다음 페이지 커서. 그대로 `cursor`로 보낸다. 마지막 페이지면 `null` |
| `has_more` | boolean | O | 다음 페이지가 있는지 |
| `items` | array<[ExecutionArtifactSummaryResponse](#schema-executionartifactsummaryresponse)> | O | 이번 페이지 항목 |

<a id="schema-executionartifactresponse"></a>
#### `ExecutionArtifactResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `artifact_id` | string(uuid) | O | Artifact ID |
| `name` | string | O | 이름 |
| `description` | string \| null | O | 설명 |
| `type` | `DATASET` \| `NOTEBOOK` \| `REPORT` \| `PLOT` \| `MODEL` \| `METRIC` \| `LOG` \| `OTHER` | O | Artifact 유형 |
| `status` | `AVAILABLE` \| `INCOMPLETE` \| `DELETED` | O | `AVAILABLE`(파일 있음, 실행 성공 뜻 아님), `INCOMPLETE`, `DELETED` |
| `produced_by` | [ArtifactProducer](#schema-artifactproducer) | O | 만든 Execution·Attempt·Step. 노트북은 Step 값이 `null` |
| `lineage` | [ArtifactLineage](#schema-artifactlineage) | O | 추적 정보 |
| `storage` | [ArtifactStorage](#schema-artifactstorage) | O | 저장 위치 |
| `metadata` | object | O | Artifact metadata |

<a id="schema-executionoperationreceipt"></a>
#### `ExecutionOperationReceipt`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `operation_id` | string(uuid) | O | Operation ID |
| `steps` | array<[ExecutionStepReceipt](#schema-executionstepreceipt)> | O | 접수된 Step의 sequence와 발급된 step_id |

<a id="schema-executioncommandstate"></a>
#### `ExecutionCommandState`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `status` | `QUEUED` \| `RUNNING` \| `WAITING_FOR_OPERATION` \| `FINALIZING` \| `CANCEL_REQUESTED` \| `CANCELLED` \| `SUCCEEDED` \| `FAILED` | O | 명령 반영 직후 Execution 상태 |
| `version` | integer | O | 명령 반영 직후 상태 버전 |

<a id="schema-errordetail"></a>
#### `ErrorDetail`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `code` | string | O | 오류 코드(아래 표) |
| `message` | string | O | 사람이 읽는 메시지 |
| `details` | array<[ValidationIssue](#schema-validationissue)> \| null |  | 필드별 문제 목록 |

<a id="schema-executionsummaryresponse"></a>
#### `ExecutionSummaryResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `execution_id` | string(uuid) | O | Execution ID |
| `operation_mode` | `SINGLE` \| `MULTI` | O | `SINGLE` 또는 `MULTI` |
| `trigger_type` | `INTERACTIVE` \| `BATCH` | O | `INTERACTIVE` 또는 `BATCH`(`trigger.type`과 같은 값) |
| `trigger` | [ExecutionTrigger](#schema-executiontrigger) | O | 트리거 |
| `tenant_id` | string | O | Execution의 tenant. 이벤트 Stream 이름을 정한다. 바뀌지 않는다 |
| `context` | [ExecutionContext](#schema-executioncontext) | O | 소유 범위 |
| `metadata` | object | O | 제출 최상위 metadata(민감 키는 `[REDACTED]`) |
| `state` | [ExecutionCommandState](#schema-executioncommandstate) | O | 상태 |
| `lifecycle` | [Lifecycle](#schema-lifecycle) | O | 수명 시각 |
| `step_count` | integer | O | Step 수 |

<a id="schema-executiontrigger"></a>
#### `ExecutionTrigger`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `type` | `INTERACTIVE` \| `BATCH` | O | `INTERACTIVE` 또는 `BATCH` |

<a id="schema-executioncontext"></a>
#### `ExecutionContext`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `user_id` | string | O | 소유 사용자(제출 때 `X-User-Id`) |
| `project_id` | string \| null | O | 제출 때 `X-Project-Id` 또는 `null` |
| `session_id` | string \| null | O | 제출 때 `X-Session-Id` 또는 `null` |

<a id="schema-executionruntime"></a>
#### `ExecutionRuntime`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `type` | `JUPYTER` | O | Runtime 종류(`JUPYTER`) |
| `pool` | `INTERACTIVE` \| `BATCH` | O | 트리거로 정해진 Pool |
| `profile` | string | O | 요청한 커널 프로파일 |
| `target_id` | string(uuid) \| null | O | 할당된 Runtime Target. 할당 전이나 FROM_START 재시도 준비 중에는 `null`. 등록 삭제 후에도 과거 ID가 남는다 |
| `kernel_id` | string \| null | O | Jupyter 커널 ID(호출자 `X-Session-Id`와 무관). 생성 전·정리 후 `null` |

<a id="schema-executionstate"></a>
#### `ExecutionState`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `status` | `QUEUED` \| `RUNNING` \| `WAITING_FOR_OPERATION` \| `FINALIZING` \| `CANCEL_REQUESTED` \| `CANCELLED` \| `SUCCEEDED` \| `FAILED` | O | Execution 상태. `CANCELLED`, `SUCCEEDED`, `FAILED`가 terminal |
| `version` | integer | O | 상태가 바뀔 때마다 오르는 버전. operations·finalize의 `expected_version` |
| `cancellation_reason` | string \| null | O | 취소 사유 또는 `null` |
| `finalized_by` | `AGENT` \| `WAIT_TIMEOUT` \| `MAX_RUNTIME` \| null |  | 무엇이 끝냈나: `AGENT`(finalize), `WAIT_TIMEOUT`(MULTI 대기 기한), `MAX_RUNTIME`(전체 수명). 진행 중·SINGLE 정상 종료·취소는 `null` |

<a id="schema-workspaceresponse"></a>
#### `WorkspaceResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `path` | string \| null | O | workspace 상대경로. 준비 전 `null` |
| `notebook_path` | string \| null | O | 실행 노트북 상대경로. 준비 전 `null` |
| `notebook_projection` | [NotebookProjectionResponse](#schema-notebookprojectionresponse) | O | 노트북 반영 상태. 반영 실패는 실행 성공에 영향을 주지 않는다 |

<a id="schema-failureresponse"></a>
#### `FailureResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `type` | `TOOL_ERROR` \| `INFRASTRUCTURE_ERROR` \| `WORKER_SHUTDOWN` \| `RUNTIME_UNAVAILABLE` \| `LEASE_EXPIRED` \| `INTERNAL_ERROR` \| `OPERATION_WAIT_TIMEOUT` \| `OPERATION_TIMEOUT` \| `STEP_TIMEOUT` \| `EXECUTION_TIMEOUT` \| `QUEUE_TIMEOUT` \| `OUTPUT_LIMIT_EXCEEDED` \| `RUNTIME_SESSION_LOST` \| `COMPLETION_FAILED` | O | 실패 유형 |
| `message` | string | O | 실패 메시지 |

<a id="schema-retryresponse"></a>
#### `RetryResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `strategy` | `NOT_RETRYABLE` \| `FROM_FAILED_STEP` \| `FROM_START` | O | `NOT_RETRYABLE`, `FROM_FAILED_STEP`(보존 커널에서 실패 Step부터), `FROM_START`(새 커널에서 처음부터). Executor가 정한다 |
| `count` | integer | O | 요청된 retry 횟수 |
| `from_sequence` | integer \| null | O | 다음 retry가 시작할 sequence |
| `retained_runtime_session_until` | string(date-time) \| null | O | 실패 커널 보존 만료 시각(기본 1시간) 또는 `null` |

<a id="schema-recoveryresponse"></a>
#### `RecoveryResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `count` | integer | O | Worker 장애로 소유권이 복구된 횟수 |
| `runtime_session_cleanup_status` | `NOT_REQUIRED` \| `PENDING` \| `SUCCEEDED` \| `FAILED` | O | 버려진 커널 정리 상태 |
| `runtime_abort_status` | `NOT_REQUIRED` \| `PENDING` \| `IDLE_CONFIRMED` \| `SESSION_DELETED` \| `SESSION_MISSING` \| `FAILED` | O | 취소·중단 시 커널 중지 상태 |

<a id="schema-deadlinesresponse"></a>
#### `DeadlinesResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `operation_wait_expires_at` | string(date-time) \| null | O | MULTI 다음 Operation 대기 만료. 대기 중이 아니면 `null` |
| `execution_expires_at` | string(date-time) \| null | O | 전체 수명 만료(기본 7일). 시작 전 `null` |

<a id="schema-executionlifecycleresponse"></a>
#### `ExecutionLifecycleResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `operation_mode` | `SINGLE` \| `MULTI` | O | 제출 값 |
| `operation_wait_timeout_seconds` | integer \| null | O | 제출 값(SINGLE은 `null`) |
| `started_at` | string(date-time) \| null | O | 시작 시각 |
| `finished_at` | string(date-time) \| null | O | 종료 시각. 진행 중이면 `null` |

<a id="schema-executionresultexecutionresponse"></a>
#### `ExecutionResultExecutionResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `execution_id` | string(uuid) | O | Execution ID |
| `state` | [ExecutionCommandState](#schema-executioncommandstate) | O | 상태 |

<a id="schema-executionresultoperationresponse"></a>
#### `ExecutionResultOperationResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `operation_id` | string(uuid) | O | Operation ID |
| `operation_number` | integer | O | Execution 안의 Operation 순번(1부터) |
| `sequence_range` | [OperationSequenceRange](#schema-operationsequencerange) | O | 이 Operation이 담은 Step sequence 범위 |
| `result` | [OperationResult](#schema-operationresult) | O | 결과 |
| `lifecycle` | [Lifecycle](#schema-lifecycle) | O | 수명 시각 |
| `steps` | array<[ExecutionResultStepResponse](#schema-executionresultstepresponse)> | O | 현재 Step 결과 |

<a id="schema-executionattemptresponse"></a>
#### `ExecutionAttemptResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `attempt_id` | string(uuid) | O | Attempt ID |
| `execution_id` | string(uuid) | O | Execution ID |
| `attempt_number` | integer | O | Attempt 순번(1부터) |
| `state` | [AttemptState](#schema-attemptstate) | O | 상태 |
| `failure` | [FailureResponse](#schema-failureresponse) \| null | O | 실패 정보. 실패가 아니면 `null` |
| `lifecycle` | [Lifecycle](#schema-lifecycle) | O | 수명 시각 |
| `step_count` | integer | O | Step 수 |

<a id="schema-executionartifactsummaryresponse"></a>
#### `ExecutionArtifactSummaryResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `artifact_id` | string(uuid) | O | Artifact ID |
| `name` | string | O | 이름 |
| `type` | `DATASET` \| `NOTEBOOK` \| `REPORT` \| `PLOT` \| `MODEL` \| `METRIC` \| `LOG` \| `OTHER` | O | Artifact 유형 |
| `status` | `AVAILABLE` \| `INCOMPLETE` \| `DELETED` | O | `AVAILABLE`, `INCOMPLETE`, `DELETED` |
| `produced_by` | [ArtifactProducer](#schema-artifactproducer) | O | 만든 Execution·Attempt·Step. 노트북은 Step 값이 `null` |
| `storage` | [ArtifactStorageSummary](#schema-artifactstoragesummary) | O | 저장 위치 |

<a id="schema-notebookcellsummaryresponse"></a>
#### `NotebookCellSummaryResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `index` | integer | O | 셀 index(0부터) |
| `id` | string \| null | O | 셀 ID |
| `type` | string | O | `code`, `markdown` 등 |
| `execution_count` | integer \| null | O | Jupyter 실행 번호 |
| `source_preview` | string | O | 소스 앞부분 |
| `source_truncated` | boolean | O | 미리보기가 잘렸는지 |
| `line_count` | integer | O | 소스 줄 수 |
| `metadata` | object | O | 셀 metadata |
| `output_summary` | [OutputSummary](#schema-outputsummary) | O | 출력 요약 |

<a id="schema-notebookcellresponse"></a>
#### `NotebookCellResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `index` | integer | O | 셀 index(0부터) |
| `id` | string \| null | O | 셀 ID |
| `type` | string | O | `code`, `markdown` 등 |
| `execution_count` | integer \| null | O | Jupyter 실행 번호 |
| `source` | string | O | 셀 소스 전체 |
| `line_count` | integer | O | 소스 줄 수 |
| `metadata` | object | O | 셀 metadata |
| `output_summary` | [OutputSummary](#schema-outputsummary) | O | 출력 요약 |
| `outputs` | array<object> | O | Jupyter 출력 전체 |

<a id="schema-notebookpage"></a>
#### `NotebookPage`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `start_index` | integer | O | 시작 셀 index |
| `limit` | integer | O | 요청한 개수 |
| `total_count` | integer | O | 전체 셀 수 |
| `has_more` | boolean | O | 다음 셀이 더 있는지 |

<a id="schema-executionoperationsummaryresponse"></a>
#### `ExecutionOperationSummaryResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `operation_id` | string(uuid) | O | Operation ID |
| `operation_number` | integer | O | Execution 안의 Operation 순번(1부터) |
| `sequence_range` | [OperationSequenceRange](#schema-operationsequencerange) | O | 이 Operation이 담은 Step sequence 범위 |
| `result` | [OperationResult](#schema-operationresult) | O | 결과 |
| `lifecycle` | [Lifecycle](#schema-lifecycle) | O | 수명 시각 |
| `step_count` | integer | O | Step 수 |

<a id="schema-executiondiagnosticresponse"></a>
#### `ExecutionDiagnosticResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `id` | string(uuid) | O | 진단 ID |
| `execution_id` | string(uuid) | O | Execution ID |
| `attempt_id` | string(uuid) \| null | O | Attempt ID |
| `operation_id` | string(uuid) \| null | O | Operation ID |
| `step_id` | string(uuid) \| null | O | Step ID(Executor 발급) |
| `step_sequence` | integer \| null | O | 관련 Step sequence 또는 `null` |
| `fencing_token` | integer | O | 결과를 기록한 Worker lease 세대 번호 |
| `occurred_at` | string(date-time) | O | 발생 시각 |
| `diagnostic` | [RuntimeDiagnosticResponse](#schema-runtimediagnosticresponse) | O | 진단 내용 |

<a id="schema-executionstepsummaryresponse"></a>
#### `ExecutionStepSummaryResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `step_id` | string(uuid) | O | Step ID(Executor 발급) |
| `execution_id` | string(uuid) | O | Execution ID |
| `sequence` | integer | O | Step 순번 |
| `lineage` | [ToolReference](#schema-toolreference) | O | 추적 정보 |
| `result` | [StepResultSummary](#schema-stepresultsummary) | O | 결과 |
| `lifecycle` | [Lifecycle](#schema-lifecycle) | O | 수명 시각 |

<a id="schema-stepsourceresponse"></a>
#### `StepSourceResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `type` | `INLINE` \| `PATH` | O | `INLINE` 또는 `PATH` |
| `path` | string \| null | O | PATH 원본 경로. INLINE은 `null` |
| `sha256` | string | O | PATH 원본 SHA-256. INLINE은 `null` |

<a id="schema-toolreference"></a>
#### `ToolReference`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `skill_name` | string \| null | O | Skill 이름 |
| `tool_name` | string \| null | O | Tool 이름 |
| `input_parameters` | object | O | Tool 입력값 |

<a id="schema-stepresult"></a>
#### `StepResult`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `status` | `PENDING` \| `RUNNING` \| `SUCCEEDED` \| `FAILED` \| `SKIPPED` \| `CANCELLED` | O | Step 상태 |
| `output_summary` | [OutputSummary](#schema-outputsummary) | O | 출력 요약 |
| `error_message` | string \| null | O | 실패 메시지 또는 `null` |
| `result_ref` | [StepResultReference](#schema-stepresultreference) \| null | O | 공유 PV의 Step 결과 manifest 참조. 결과가 없으면 `null` |

<a id="schema-lifecycle"></a>
#### `Lifecycle`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `started_at` | string(date-time) \| null | O | 시작 시각 |
| `finished_at` | string(date-time) \| null | O | 종료 시각. 진행 중이면 `null` |

<a id="schema-operationsequencerange"></a>
#### `OperationSequenceRange`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `first` | integer | O | 첫 sequence |
| `last` | integer | O | 마지막 sequence |

<a id="schema-operationresult"></a>
#### `OperationResult`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `status` | `QUEUED` \| `RUNNING` \| `SUCCEEDED` \| `FAILED` \| `CANCELLED` | O | Operation 상태 |
| `error_message` | string \| null | O | 실패 메시지 또는 `null` |

<a id="schema-attemptstate"></a>
#### `AttemptState`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `status` | `RUNNING` \| `WAITING` \| `SUCCEEDED` \| `FAILED` \| `CANCELLED` | O | Attempt 상태 |

<a id="schema-attemptruntime"></a>
#### `AttemptRuntime`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `type` | `JUPYTER` | O | Runtime 종류 |
| `profile` | string | O | 커널 프로파일 |
| `target_id` | string(uuid) | O | 실행한 Runtime Target |
| `kernel_id` | string \| null | O | Jupyter 커널 ID |

<a id="schema-attemptlease"></a>
#### `AttemptLease`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `owner` | string \| null | O | lease를 가진 Worker |
| `expires_at` | string(date-time) \| null | O | lease 만료 시각 |
| `heartbeat_at` | string(date-time) \| null | O | 마지막 Worker 생존 신호. 코드 진척이 아니다 |

<a id="schema-attemptrecovery"></a>
#### `AttemptRecovery`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `retry_strategy` | `NOT_RETRYABLE` \| `FROM_FAILED_STEP` \| `FROM_START` | O | 이 Attempt의 재시도 전략 |
| `runtime_session_cleanup_status` | `NOT_REQUIRED` \| `PENDING` \| `SUCCEEDED` \| `FAILED` | O | 버려진 커널 정리 상태 |
| `runtime_abort_status` | `NOT_REQUIRED` \| `PENDING` \| `IDLE_CONFIRMED` \| `SESSION_DELETED` \| `SESSION_MISSING` \| `FAILED` | O | 취소·중단 시 커널 중지 상태 |

<a id="schema-executionstepattemptsummaryresponse"></a>
#### `ExecutionStepAttemptSummaryResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `step_attempt_id` | string(uuid) | O | Step Attempt ID(이 Attempt에서 실행된 Step 한 번) |
| `execution_step_id` | string(uuid) | O | Step ID |
| `sequence` | integer | O | Step 순번 |
| `tool` | [ToolReference](#schema-toolreference) | O | Skill·Tool 정보 |
| `result` | [StepResult](#schema-stepresult) | O | 결과 |
| `lifecycle` | [Lifecycle](#schema-lifecycle) | O | 수명 시각 |

<a id="schema-executioneventresponse"></a>
#### `ExecutionEventResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `event_id` | string(uuid) | O | 이벤트 ID(중복 제거 키) |
| `execution_id` | string(uuid) | O | Execution ID |
| `event_sequence` | integer | O | Execution별 1부터 증가하는 순번 |
| `schema_version` | string | O | 스키마 버전(`1.0`) |
| `event_type` | string | O | 이벤트 유형 |
| `payload` | object | O | 이벤트 payload(이벤트 문서 참고) |
| `occurred_at` | string(date-time) | O | 발생 시각 |
| `delivery` | [EventDelivery](#schema-eventdelivery) \| null | O | Redis 전송 기록. 오래된 이벤트는 정리돼 `null` |

<a id="schema-artifactproducer"></a>
#### `ArtifactProducer`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `execution_id` | string(uuid) | O | Execution ID |
| `execution_attempt_id` | string(uuid) \| null | O | Attempt ID |
| `execution_step_id` | string(uuid) \| null | O | Step ID |
| `execution_step_attempt_id` | string(uuid) \| null | O | Step Attempt ID |

<a id="schema-artifactlineage"></a>
#### `ArtifactLineage`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `parent_artifact_id` | string(uuid) \| null | O | 부모 Artifact |
| `external_parent_asset_id` | string \| null | O | 외부 부모 자산 ID |

<a id="schema-artifactstorage"></a>
#### `ArtifactStorage`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `type` | `PV` \| `S3` | O | `PV` 또는 `S3` |
| `uri` | string | O | 저장 URI |
| `relative_path` | string \| null | O | Jupyter PV 기준 상대경로 |
| `media_type` | string \| null | O | MIME 유형 |
| `size_bytes` | integer \| null | O | 파일 크기 |
| `checksum_sha256` | string \| null | O | 등록 시점 SHA-256 |

<a id="schema-executionstepreceipt"></a>
#### `ExecutionStepReceipt`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `sequence` | integer | O | 요청한 sequence |
| `step_id` | string(uuid) | O | 발급된 Step ID |

<a id="schema-validationissue"></a>
#### `ValidationIssue`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `location` | array<string \| integer> | O | `body`/`header`/`query`/`path` + 필드 경로 |
| `message` | string | O | 메시지 |
| `type` | string | O | 안정적인 snake_case 사유 |
| `context` | object \| null |  | 한도와 실제 값 등 근거 |

<a id="schema-notebookprojectionresponse"></a>
#### `NotebookProjectionResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `status` | `NOT_STARTED` \| `PENDING` \| `SUCCEEDED` \| `FAILED` | O | `NOT_STARTED`/`PENDING`/`SUCCEEDED`/`FAILED` |
| `attempt_count` | integer | O | 반영 시도 횟수 |
| `error_message` | string \| null | O | 마지막 반영 오류 |
| `projected_at` | string(date-time) \| null | O | 마지막으로 반영된 시각 |

<a id="schema-executionresultstepresponse"></a>
#### `ExecutionResultStepResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `step_id` | string(uuid) | O | Step ID(Executor 발급) |
| `sequence` | integer | O | Step 순번 |
| `lineage` | [ToolReference](#schema-toolreference) | O | 추적 정보 |
| `result` | [StepResult](#schema-stepresult) | O | 결과 |
| `lifecycle` | [Lifecycle](#schema-lifecycle) | O | 수명 시각 |

<a id="schema-artifactstoragesummary"></a>
#### `ArtifactStorageSummary`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `type` | `PV` \| `S3` | O | `PV` 또는 `S3` |
| `media_type` | string \| null | O | MIME 유형 |
| `size_bytes` | integer \| null | O | 파일 크기 |

<a id="schema-outputsummary"></a>
#### `OutputSummary`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `output_count` | integer | O | Jupyter 출력 개수 (≥0) |
| `output_types` | object | O | 출력 유형별 개수(`stream`, `display_data`, `error` 등) |
| `stream_names` | array<string> | O | 나온 stream 이름(`stdout`, `stderr`) |
| `mime_types` | array<string> | O | 나온 MIME 유형 |
| `has_image` | boolean | O | 이미지 출력이 있는지 |
| `image_count` | integer | O | 이미지 출력 개수 (≥0) |
| `has_error` | boolean | O | 오류 출력이 있는지 |

<a id="schema-runtimediagnosticresponse"></a>
#### `RuntimeDiagnosticResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `code` | string | O | 진단 코드(예: `NOTEBOOK_NOT_REFRESHED`) |
| `phase` | string | O | 오류를 관측한 처리 단계 |
| `category` | `EXECUTION` \| `OUTPUT` \| `NOTEBOOK` \| `ARTIFACT` \| `CLEANUP` | O | `EXECUTION`, `OUTPUT`, `NOTEBOOK`, `ARTIFACT`, `CLEANUP` |
| `origin` | `RUNTIME` \| `RESULT_STORAGE` \| `EXECUTOR` \| `UNKNOWN` | O | `RUNTIME`, `RESULT_STORAGE`, `EXECUTOR`, `UNKNOWN` |
| `severity` | `ERROR` | O | `ERROR` |
| `message` | string | O | 진단 메시지 |
| `causes` | array<[DiagnosticCauseResponse](#schema-diagnosticcauseresponse)> | O | 원인 예외 체인(개수 제한) |
| `causes_truncated` | boolean | O | 원인이 잘렸는지 |

<a id="schema-stepresultsummary"></a>
#### `StepResultSummary`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `status` | `PENDING` \| `RUNNING` \| `SUCCEEDED` \| `FAILED` \| `SKIPPED` \| `CANCELLED` | O | Step 상태 |
| `output_summary` | [OutputSummary](#schema-outputsummary) | O | 출력 요약 |
| `error_message` | string \| null | O | 실패 메시지 또는 `null` |

<a id="schema-stepresultreference"></a>
#### `StepResultReference`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `storage` | `SHARED_PV` |  | `SHARED_PV` (기본 `"SHARED_PV"`) |
| `execution_id` | string(uuid) | O | Execution ID |
| `step_id` | string(uuid) | O | Step ID(Executor 발급) |
| `attempt_id` | string(uuid) | O | Attempt ID |
| `fencing_token` | integer | O | 결과를 기록한 Worker lease 세대 번호 |
| `relative_path` | string | O | manifest 파일의 공유 루트 기준 상대경로 |
| `checksum_sha256` | string | O | manifest 파일의 SHA-256. 읽은 뒤 검증한다 |
| `size_bytes` | integer | O | manifest 파일 자체의 크기 |
| `complete` | boolean | O | 출력이 모두 저장됐는지. 실행 성공 여부가 아니다 |
| `representation_count` | integer | O | 저장된 MIME representation 총개수 |
| `total_size_bytes` | integer | O | 출력 representation 파일 크기의 합 |

<a id="schema-eventdelivery"></a>
#### `EventDelivery`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `status` | `PENDING` \| `PUBLISHED` | O | `PENDING` 또는 `PUBLISHED` |
| `attempt_count` | integer | O | 시도 횟수 |
| `available_at` | string(date-time) | O | 다음 전송 가능 시각 |
| `published_at` | string(date-time) \| null | O | Redis에 발행된 시각 |
| `last_error` | string \| null | O | 마지막 오류 또는 `null` |

<a id="schema-diagnosticcauseresponse"></a>
#### `DiagnosticCauseResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `exception_type` | string | O | 예외 타입 |
| `message` | string | O | 예외 메시지 |
| `errno` | integer \| null | O | OS errno 또는 `null` |
