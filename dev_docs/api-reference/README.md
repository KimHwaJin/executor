# Executor API 레퍼런스

Kubernetes에 배포된 Executor를 호출할 때 필요한 내용만 담았다. 요청·응답 표는
코드가 생성한 OpenAPI와 Pydantic 계약에서 그대로 뽑았고, 의미 설명은 코드와 대조해 붙였다.

## 문서 구성

| 문서 | 대상 | 개수 |
| --- | --- | ---: |
| [01-executions.md](01-executions.md) | 실행 REST API(명령·조회·노트북·이력·Artifact) | 25 |
| [02-admin.md](02-admin.md) | 관리자 REST API(Runtime Target·Pool 9, 유지보수 6) | 15 |
| [03-health.md](03-health.md) | 헬스체크(`/healthz`, `/readyz`, `/workerz`) | 3 |
| [04-mcp.md](04-mcp.md) | MCP 엔드포인트 `POST /mcp`와 도구 | 1 (도구 28) |
| [05-events.md](05-events.md) | Redis Stream 이벤트 | 8종 |
| [agent-guide.md](agent-guide.md) | 코딩 에이전트용 연동 가이드(영어, 한 파일). 한글판 [agent-guide.ko.md](agent-guide.ko.md) | - |
| [openapi.json](openapi.json) | 코드에서 생성한 OpenAPI 3.1(REST 40개, 스키마 144개) | - |

HTTP 엔드포인트 합계 **44개**(실행 25 + 관리자 15 + 헬스 3 + MCP 1).

## 배포 환경 (Kubernetes)

이 문서는 Kubernetes에 배포된 Executor를 다른 서비스가 호출하는 상황을 기준으로 쓴다. Executor는 Deployment `executor`와
ClusterIP Service `executor`(포트 `8000`)로 떠 있다. 하나의 이미지가 API와 Worker를 함께 돌린다.

| 항목 | 값 |
| --- | --- |
| 클러스터 내부 URL | 같은 namespace: `http://executor:8000`, 다른 namespace: `http://executor.<namespace>.svc.cluster.local:8000` |
| 클러스터 밖 | 플랫폼이 관리하는 Gateway/Ingress 주소(환경마다 다름) |
| REST 기준 경로 | `{URL}/api/v1` |
| MCP | dev에서만 `POST {URL}/mcp`. stg·prd는 꺼져 있다(404) |
| API 문서 | dev에서만 `/docs`, `/redoc`, `/openapi`. stg·prd는 404 |
| 헬스 | `/healthz`, `/readyz`(인증 없음), `/workerz`(admin key) |
| 이벤트 | Redis Stream `executor.events.{tenant_id}`, 7일 보관 |

**API key.** 환경(dev·stg·prd)마다 따로 만들고 운영자가 호출 서비스에 직접 전달한다. Executor에는 해시만 있다.

| client | 쓰는 곳 | tenant | 권한 |
| --- | --- | --- | --- |
| `pr-agentic-service` | Agent 서비스 | 기본 `dtest-agent-service`. relay key라 `X-Tenant-Id`로 다른 tenant를 대신할 수 있다 | 실행 API |
| `pr-batch` | 배치 | 기본 tenant가 없다. **`X-Tenant-Id` 필수**(없으면 `400 INVALID_IDENTITY`) | 실행 API |
| `ops` | 운영자 | - | 관리자 API(Runtime Target·Pool·유지보수, `/workerz`) |

**공유 스토리지.** Executor와 호출 서비스는 같은 RWX PVC를 `/mnt/data`에 마운트하고, 프로젝트 루트는
`/mnt/data/dtest`(`STORAGE_ROOT`)다. 경로 값은 모두 이 루트 기준 상대경로다.

- 입력(PATH source): 호출측이 자기 폴더에 써 둔 파일. 예: `agent/...`, `batch/inputs/...`
- 출력(`result_ref.relative_path` 등): Executor가 쓰는 `executor/executions/...`
- 절대경로와 `..`는 받지 않는다. 노트북·Artifact 원문은 PV가 없어도 노트북·Artifact API로 읽을 수 있다.

## 공통 규칙

### 기본

- 기준 경로 `/api/v1`. 바디는 `Content-Type: application/json`.
- 명령 API는 `202 Accepted`를 돌려준다. 저장됐다는 뜻이지 실행이 끝났다는 뜻이 아니다. 진행은 이벤트와 조회 API로 본다.
- 시각은 UTC ISO-8601, 시간 값은 초 단위 정수.
- 모든 요청 모델은 모르는 필드를 거부한다(`422 REQUEST_VALIDATION_ERROR`). 바디에 `actor`, `context`, `tenant_id`, `user_id`를 넣지 않는다.
- 바디 최대 크기 `REQUEST_BODY_MAX_BYTES`(기본 30MB). 넘으면 `413 REQUEST_BODY_TOO_LARGE`. 큰 코드는 PATH source로 보낸다.

### 값의 위치

| 역할 | 위치 | 예 |
| --- | --- | --- |
| 누가, 어느 tenant를 위해 | 헤더 | `X-Api-Key`, `X-Tenant-Id`, `X-User-Id`(제출만 `X-Project-Id`, `X-Session-Id` 추가) |
| 어느 리소스 | 경로 | `execution_id`, `operation_id`, `attempt_id`, `step_id`, `artifact_id`, `target_id`, `run_id`, `cell_index` |
| 목록 필터 | 쿼리 | `user_id`, `project_id`, `session_id`, `status`, `trigger_type`, `cursor`, `limit` |
| 무엇을 할지 | 바디 | `idempotency_key`, `expected_version`, `spec`, `reason`, `metadata` |

### 헤더와 인증

| 헤더 | 필수 | 규칙 |
| --- | --- | --- |
| `X-Api-Key` | 헬스(`/healthz`, `/readyz`)·문서 경로를 뺀 모든 호출 | `AUTH_CLIENTS`에 등록된 key(SHA-256 비교). 없거나 틀리면 `401 UNAUTHENTICATED` |
| `X-Tenant-Id` | 선택 | relay key만, 그 key의 `allowed_tenants` 안에서(`["*"]`는 전부). 없으면 key의 기본 tenant. 형식 `^[a-z][a-z0-9-]{1,62}[a-z0-9]$`, 예약어 `dlq`·`work` 금지. 형식 오류 `400 INVALID_IDENTITY`, 허용 밖 `403 FORBIDDEN`. 기본 tenant도 헤더도 없으면 `400` |
| `X-User-Id` | 쓰기 필수 | 소문자로 바꾼 뒤 `^[a-z0-9][a-z0-9._-]{0,63}$`. 쓰기에 없으면 `400 INVALID_IDENTITY`. 읽기에서 생략하면 tenant 전체를 보고, 목록은 쿼리 `user_id`로 거를 수 있다. `created_by`/`updated_by`로 기록 |
| `X-Project-Id` | 선택 | `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`, `unscoped` 금지. **제출 때만 저장**, 다른 호출은 형식만 검사 |
| `X-Session-Id` | 선택 | 형식은 `X-Project-Id`와 같고 `X-Project-Id`가 있어야 한다. 제출 때만 저장 |
| `X-Request-ID` | 선택 | `^[A-Za-z0-9._:-]{1,128}$`면 Executor 로그에 함께 남는다 |

보이는 범위:

- Execution은 tenant가 호출자 tenant이고, `X-User-Id`를 보냈다면 소유자가 그 사용자일 때만 보인다. 아니면 존재해도 `404`. Artifact는 그 Execution 기준.
- 쓰기는 `X-User-Id`가 필수이고 그 사용자가 소유자여야 한다.
- 관리자 경로(`/api/v1/runtime-targets*`, `/api/v1/runtime-pools`, `/api/v1/maintenance*`, `/workerz`, MCP `runtime_target_*`)는 `admin: true` key만(`403 FORBIDDEN`).

### 멱등성

- 명령 바디에는 `idempotency_key`(1~255자)가 있다(probe·등록 삭제 제외).
- 제출 키는 tenant 단위, 기존 Execution에 대한 명령(cancel, retry, operations, finalize, artifacts) 키는 Execution 단위.
- 같은 키·같은 요청은 처음 결과를 다시 준다. 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT`. 같음은 바디와 행위자(`X-User-Id`, client)로 판단한다.

### 버전

`state.version`은 상태가 바뀔 때마다 오른다. MULTI의 operations·finalize는 `expected_version`이
현재 값과 같아야 하며 다르면 `409`. 최신 값은 상태 조회나 `execution.operation_completed`의 `continuation.expected_version`에서 얻는다.

### 페이지

목록 응답은 `{"items": [...], "next_cursor": "...", "has_more": true}`. 다음 페이지는 `next_cursor`를 그대로 `cursor`로 보낸다.
커서를 고치거나 다음 페이지에서 필터를 바꾸면 `422 INVALID_CURSOR`. `limit` 범위는 각 API 표에 있다(보통 1~200, 기본 100; 이벤트 1~500 기본 200; Artifact 1~1000).

### 감사 필드

대부분 응답에 `created_by`, `created_by_client`, `updated_by`, `updated_by_client`, `created_at`, `updated_at`이 있다.
`*_by`는 `X-User-Id`, `*_by_client`는 API key의 client 이름이다.

### 오류

모든 오류 응답은 같은 모양이다(없는 경로·메서드 포함).

```json
{
  "error": {
    "code": "INVALID_EXECUTION_SPEC",
    "message": "…",
    "details": [
      {"location": ["body", "operation", "spec", "steps", 0, "sequence"], "message": "…", "type": "…", "context": {"limit": 100}}
    ]
  }
}
```

`details[]`의 `location`은 `body`/`header`/`query`/`path`로 시작하는 필드 경로, `type`은 안정적인 snake_case 사유,
`context`는 근거 값(한도 등)이다. MCP에서는 `[CODE] message details=[...]` 문자열이다.

| HTTP | code | 뜻 | 호출자 대응 |
| --- | --- | --- | --- |
| 400 | `INVALID_IDENTITY` | 헤더 형식 오류, 쓰기에 `X-User-Id` 없음, tenant 없음 | 헤더 수정 |
| 400 | `RUNTIME_TARGET_CONFIGURATION_ERROR` | Target 설정 오류 | 설정 수정 |
| 400 | `ARTIFACT_REGISTRATION_ERROR` | Artifact 등록 실패 | 요청 확인 |
| 401 | `UNAUTHENTICATED` | key 없음·틀림 | key 확인 |
| 403 | `FORBIDDEN` | tenant 허용 밖, admin 전용 경로 | 권한 확인 |
| 404 | `EXECUTION_NOT_FOUND` | 없거나 보이지 않음 | id·tenant·사용자 확인 |
| 404 | `EXECUTION_OPERATION_NOT_FOUND`, `EXECUTION_ATTEMPT_NOT_FOUND`, `ARTIFACT_NOT_FOUND`, `NOTEBOOK_CELL_NOT_FOUND`, `RUNTIME_TARGET_NOT_FOUND`, `MAINTENANCE_RUN_NOT_FOUND` | 대상 없음 | id 확인 |
| 404 | `NOT_FOUND` | 없는 경로 | 경로 확인 |
| 405 | `METHOD_NOT_ALLOWED` | 없는 메서드 | 메서드 확인 |
| 409 | `INVALID_STATE_TRANSITION` | 지금 상태에서 불가(terminal, 대기 아님, retry 조건 불충족 등) | 상태 재조회 후 판단, 그대로 재시도 금지 |
| 409 | `EXECUTION_VERSION_CONFLICT` | `expected_version` 불일치 | 상태 재조회 후 최신 버전으로 |
| 409 | `IDEMPOTENCY_CONFLICT` | 같은 키로 다른 요청 | 새 키 사용 |
| 409 | `PERSISTENCE_CONFLICT` | 동시 변경 충돌 | 재조회 후 재시도 |
| 409 | `EXECUTION_NOTEBOOK_NOT_AVAILABLE` | 노트북 아직 없음 | 나중에 다시 |
| 409 | `ARTIFACT_CONTENT_UNAVAILABLE` | 파일 없음·읽기 불가 | 상세의 `status` 확인 |
| 409 | `RUNTIME_TARGET_PURGE_CONFLICT`, `RUNTIME_TARGET_ENDPOINT_CONFLICT`, `MAINTENANCE_RUN_CONFLICT` | 관리 작업 충돌 | 조건 확인 |
| 413 | `REQUEST_BODY_TOO_LARGE` | 바디 초과 | PATH source 사용 |
| 416 | `ARTIFACT_RANGE_NOT_SATISFIABLE` | Range 밖(`Content-Range: bytes */{size}`) | Range 수정 |
| 422 | `REQUEST_VALIDATION_ERROR` | 형식(필수·타입·길이·패턴·모르는 필드) | 요청 수정 |
| 422 | `INVALID_EXECUTION_SPEC` | Spec 규칙(sequence, Step 수, 시간 제한, checksum) | 요청 수정 |
| 422 | `UNSUPPORTED_RUNTIME_PROFILE`, `UNSUPPORTED_RUNTIME_CAPABILITY` | 지원 안 하는 프로파일·기능 | 값 수정 |
| 422 | `INVALID_CURSOR` | 커서 변조·필터 변경 | 첫 페이지부터 |
| 422 | `NOTEBOOK_READ_ERROR` | 노트북 파일 읽기 실패 | diagnostics 확인 |
| 500 | `INTERNAL_ERROR` | 예기치 못한 오류 | 백오프 재시도(명령은 같은 `idempotency_key`로) |
| 503 | `DATABASE_UNAVAILABLE` | DB 연결 문제 | 백오프 재시도(명령은 같은 `idempotency_key`로) |

## 상태 값

| 대상 | 값 |
| --- | --- |
| Execution | `QUEUED`, `RUNNING`, `WAITING_FOR_OPERATION`, `FINALIZING`, `CANCEL_REQUESTED`, `CANCELLED`*, `SUCCEEDED`*, `FAILED`* (*terminal) |
| Operation | `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED` |
| Step | `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `SKIPPED`, `CANCELLED` |
| Attempt | `RUNNING`, `WAITING`, `SUCCEEDED`, `FAILED`, `CANCELLED` |
| failure.type | `TOOL_ERROR`, `INFRASTRUCTURE_ERROR`, `WORKER_SHUTDOWN`, `RUNTIME_UNAVAILABLE`, `LEASE_EXPIRED`, `INTERNAL_ERROR`, `OPERATION_WAIT_TIMEOUT`, `OPERATION_TIMEOUT`, `STEP_TIMEOUT`, `EXECUTION_TIMEOUT`, `QUEUE_TIMEOUT`, `OUTPUT_LIMIT_EXCEEDED`, `RUNTIME_SESSION_LOST`, `COMPLETION_FAILED` |
| retry.strategy | `NOT_RETRYABLE`, `FROM_FAILED_STEP`, `FROM_START` |
| Runtime Target | `ACTIVE`, `DRAINING`, `OFFLINE` |

```text
SINGLE: submit → QUEUED → RUNNING → SUCCEEDED | FAILED | CANCELLED
                                     └ FAILED이고 재시도 가능 → retry → QUEUED → RUNNING → …
MULTI : submit → QUEUED → RUNNING → WAITING_FOR_OPERATION ─┬ operations → QUEUED → RUNNING → WAITING_FOR_OPERATION …
                                                          ├ finalize   → FINALIZING → SUCCEEDED | FAILED
                                                          └ 대기 기한 초과 → FAILED(OPERATION_WAIT_TIMEOUT)
terminal 전 어느 상태든: cancel → CANCEL_REQUESTED → CANCELLED
```

## 기존 문서와의 관계

상위 폴더의 [`executor-api.md`](../executor-api.md), [`executor-events.md`](../executor-events.md)와 내용이 겹친다. 이 레퍼런스는 그 내용을
코드와 다시 대조하고, 요청·응답 필드를 코드 스키마에서 빠짐없이 뽑은 판이다. 결과 manifest 읽는 법,
Artifact Range 세부, 진단 phase 목록처럼 더 깊은 내용은 상위 `dev_docs/`의 세부 문서를 본다.
