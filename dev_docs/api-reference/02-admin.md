# 관리자 API 스펙(Runtime Target · 유지보수 · 이벤트 Stream)

운영자가 Jupyter 서버와 Executor 전체 상태를 관리하고 Redis 이벤트 Stream을 들여다보는 API 21개다. 기준 경로 `/api/v1`. **admin key(`admin: true`) 전용**이며 일반 key로 부르면 `403 FORBIDDEN`이다. 쓰기(POST)에는 운영자 `X-User-Id`가 필요하다. 공통 규칙은 [README](README.md#공통-규칙)를 본다.


## 목록

| # | 메서드 · 경로 | MCP 도구 | 용도 |
| ---: | --- | --- | --- |
| 1 | `GET /api/v1/runtime-targets` | `runtime_target_list` | [Target 목록](#ep-1) |
| 2 | `POST /api/v1/runtime-targets` | `runtime_target_upsert` | [Target 등록·수정](#ep-2) |
| 3 | `GET /api/v1/runtime-pools` | 없음 | [Pool 요약](#ep-3) |
| 4 | `GET /api/v1/runtime-targets/{target_id}` | `runtime_target_get` | [Target 상세](#ep-4) |
| 5 | `DELETE /api/v1/runtime-targets/{target_id}` | 없음 | [등록 삭제](#ep-5) |
| 6 | `GET /api/v1/runtime-targets/{target_id}/kernels` | `runtime_target_kernels` | [커널 목록과 자원 사용량](#ep-6) |
| 7 | `DELETE /api/v1/runtime-targets/{target_id}/kernels/{kernel_id}` | `runtime_target_kernel_delete` | [남겨진 커널 삭제](#ep-7) |
| 8 | `POST /api/v1/runtime-targets/{target_id}/probe` | `runtime_target_probe` | [즉시 점검](#ep-8) |
| 9 | `POST /api/v1/runtime-targets/{target_id}/drain` | `runtime_target_set_state (desired_state=DRAINING)` | [배정 중지(drain)](#ep-9) |
| 10 | `POST /api/v1/runtime-targets/{target_id}/activate` | `runtime_target_set_state (desired_state=ACTIVE)` | [배정 재개](#ep-10) |
| 11 | `POST /api/v1/runtime-targets/{target_id}/disable` | `runtime_target_disable` | [비활성화](#ep-11) |
| 12 | `GET /api/v1/maintenance` | 없음 | [유지보수 상태](#ep-12) |
| 13 | `POST /api/v1/maintenance/drain` | 없음 | [전체 수용 중지](#ep-13) |
| 14 | `POST /api/v1/maintenance/activate` | 없음 | [전체 수용 재개](#ep-14) |
| 15 | `POST /api/v1/maintenance/runs` | 없음 | [일괄 중지 Run 생성](#ep-15) |
| 16 | `GET /api/v1/maintenance/runs/{run_id}` | 없음 | [Run 상세](#ep-16) |
| 17 | `GET /api/v1/maintenance/runs/{run_id}/targets` | 없음 | [Run 대상 목록](#ep-17) |
| 18 | `GET /api/v1/event-streams` | 없음 | [Stream 전체 요약](#ep-18) |
| 19 | `GET /api/v1/event-streams/{tenant_id}` | 없음 | [Stream 상세](#ep-19) |
| 20 | `GET /api/v1/event-streams/{tenant_id}/entries` | 없음 | [Stream 항목](#ep-20) |
| 21 | `GET /api/v1/event-streams/{tenant_id}/groups/{group}/pending` | 없음 | [pending 항목](#ep-21) |

## Runtime Target·Pool

Jupyter 서버(Runtime Target) 등록과 상태 관리다. admin key만 호출할 수 있고, 쓰기에는 운영자 `X-User-Id`가 필요하다. tenant 헤더는 쓰지 않는다.

<a id="ep-1"></a>
### 1. Target 목록

`GET /api/v1/runtime-targets`

- MCP: `runtime_target_list`
- 원문 요약: List Runtime Targets and current capacity

등록된 Target과 현재 용량을 나열한다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| query | `pool` |  | `INTERACTIVE` \| `BATCH` \| null |  | Pool로 거르기 |
| query | `runtime_type` |  | `JUPYTER` \| null |  | Runtime 종류로 거르기 |
| query | `status` |  | `ACTIVE` \| `DRAINING` \| `OFFLINE` \| null |  | 상태로 거르기 |
| query | `enabled` |  | boolean \| null |  | 사용 여부로 거르기 |
| query | `cursor` |  | string \| null | 길이≤2048 | 이전 응답의 `next_cursor`를 그대로 |
| query | `limit` |  | integer | ≥1, ≤200, 기본 `100` | 페이지 크기 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`RuntimeTargetPageResponse`](#schema-runtimetargetpageresponse) | 성공 |
| 400 | [`ErrorResponse`](#schema-errorresponse) | 식별·설정 오류 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-2"></a>
### 2. Target 등록·수정

`POST /api/v1/runtime-targets`

- MCP: `runtime_target_upsert`
- 원문 요약: Register or update and immediately probe a Runtime Target

Jupyter 서버를 등록하거나(같은 이름이면 수정) 바로 점검한다. 점검이 건강해야 배정 대상이 된다.

**요청 바디** `RuntimeTargetUpsertRequest` (모르는 필드는 거부)

| 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `name` | string | O | 길이≥1, 길이≤255, `^[a-zA-Z0-9._-]+$` | Target 이름 `^[a-zA-Z0-9._-]+$`. 같은 이름이면 수정 |
| `runtime_type` | `JUPYTER` | O |  | `JUPYTER` |
| `connection_config` | object | O |  | 접속 설정. 예: `{"endpoint": "http://jupyter-01:8888"}`. 같은 endpoint를 두 이름으로 등록하면 `409 RUNTIME_TARGET_ENDPOINT_CONFLICT` |
| `credential` | string(password) \| null |  |  | Jupyter 토큰. 암호화 저장되고 응답에 나오지 않는다 |
| `pool` | `INTERACTIVE` \| `BATCH` | O |  | `INTERACTIVE` 또는 `BATCH` |
| `max_concurrent_executions` | integer \| null |  | ≥1, ≤1000 | 동시 Execution 슬롯(1~1000). 생략하면 배포 기본값 |

예:

```json
{
  "idempotency_key": "rt-jupyter-01-v1",
  "name": "jupyter-01",
  "runtime_type": "JUPYTER",
  "connection_config": {
    "endpoint": "http://jupyter-01:8888"
  },
  "credential": "<token>",
  "pool": "INTERACTIVE",
  "max_concurrent_executions": 8
}
```

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`RuntimeTargetResponse`](#schema-runtimetargetresponse) | 성공 |
| 400 | [`ErrorResponse`](#schema-errorresponse) | 식별·설정 오류 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- `credential`은 암호화 저장되고 어떤 응답에도 나오지 않는다.
- 같은 endpoint를 다른 이름으로 등록하면 `409 RUNTIME_TARGET_ENDPOINT_CONFLICT`. 설정 오류는 `400 RUNTIME_TARGET_CONFIGURATION_ERROR`.

<a id="ep-3"></a>
### 3. Pool 요약

`GET /api/v1/runtime-pools`

- MCP: 없음(REST 전용)
- 원문 요약: Get capacity and health summaries for all Runtime Pools

Pool(`INTERACTIVE`/`BATCH`)별 Target 수, 용량, 수용 상태를 준다. 페이지 없음.

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`RuntimePoolPageResponse`](#schema-runtimepoolpageresponse) | 성공 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-4"></a>
### 4. Target 상세

`GET /api/v1/runtime-targets/{target_id}`

- MCP: `runtime_target_get`
- 원문 요약: Get one Runtime Target without exposing its credential

Target 하나. 인증정보는 나오지 않는다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `target_id` | O | string(uuid) |  | Runtime Target ID |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`RuntimeTargetResponse`](#schema-runtimetargetresponse) | 성공 |
| 400 | [`ErrorResponse`](#schema-errorresponse) | 식별·설정 오류 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-5"></a>
### 5. 등록 삭제

`DELETE /api/v1/runtime-targets/{target_id}`

- MCP: 없음(REST 전용)
- 원문 요약: Remove a disabled target while preserving execution history

비활성화된 Target의 등록과 인증정보를 지운다. Execution·Attempt·이벤트·Artifact와 과거 `runtime_target_id`는 보존한다. 바디 없음.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `target_id` | O | string(uuid) |  | Runtime Target ID |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`RuntimeTargetPurgeResponse`](#schema-runtimetargetpurgeresponse) | 성공 |
| 400 | [`ErrorResponse`](#schema-errorresponse) | 식별·설정 오류 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- 조건: `enabled=false`, `OFFLINE`, 미완료 작업·활성 Attempt·해제 미확인 커널 없음. 어기면 `409 RUNTIME_TARGET_PURGE_CONFLICT`.
- 이미 삭제된 UUID는 `200`과 최초 삭제 기록. 등록도 삭제 기록도 없으면 `404 RUNTIME_TARGET_NOT_FOUND`.
- 삭제 후 같은 이름·endpoint로 다시 등록하면 새 UUID가 생긴다(새 `idempotency_key` 사용).

<a id="ep-6"></a>
### 6. 커널 목록과 자원 사용량

`GET /api/v1/runtime-targets/{target_id}/kernels`

- MCP: `runtime_target_kernels`
- 원문 요약: Get the Runtime Target's kernels, their Executions and use

Target에서 지금 도는 커널마다 메모리·CPU와 그 커널을 쓰는 Execution을 준다. Execution이 없는 커널(`execution_id`가 `null`)은 남겨진 커널이며 [남겨진 커널 삭제](#ep-7)로 지운다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `target_id` | O | string(uuid) |  | Runtime Target ID |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`RuntimeTargetKernelsResponse`](#schema-runtimetargetkernelsresponse) | 성공 |
| 400 | [`ErrorResponse`](#schema-errorresponse) | 식별·설정 오류 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |
| 503 | [`ErrorResponse`](#schema-errorresponse) | Redis 또는 Runtime Target에 닿지 못함. 나중에 다시 |

**주의**

- 요청할 때 Target에서 바로 읽는다. Target이 응답하지 않으면 `503 RUNTIME_UNAVAILABLE`, 등록이 없으면 `404 RUNTIME_TARGET_NOT_FOUND`.
- 여러 tenant의 Execution이 함께 나오므로 admin key 전용이다.

<a id="ep-7"></a>
### 7. 남겨진 커널 삭제

`DELETE /api/v1/runtime-targets/{target_id}/kernels/{kernel_id}`

- MCP: `runtime_target_kernel_delete`
- 원문 요약: Delete a kernel no Execution holds

어느 Execution도 잡고 있지 않은 커널을 Target에서 지운다. Worker가 커널을 만든 직후, 커널 ID를 기록하기 전에 죽으면 이런 커널이 남고, Jupyter가 재시작될 때까지 메모리를 차지한다. 바디 없음.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `target_id` | O | string(uuid) |  | Runtime Target ID |
| path | `kernel_id` | O | string | 길이≥1, 길이≤255 | 커널 목록의 `kernel_id` |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 204 | 없음 | 지웠거나 이미 없음 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | Target 등록이 없음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | Execution이 잡고 있거나, 15분 안에 활동한 커널 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |
| 503 | [`ErrorResponse`](#schema-errorresponse) | Target이 커널 상태를 주지 않았거나 삭제하지 못함. 나중에 다시 |

**주의**

- 상태와 Target에 관계없이 그 커널 ID를 기록한 Execution이 있으면 지우지 않는다. `409 RUNTIME_KERNEL_DELETE_CONFLICT`, `details[0].type`은 `kernel_held`, `context.execution_id`가 그 Execution이다. 실행 중이거나, MULTI Operation을 기다리거나, 실패 후 재시도를 위해 보관 중인 커널이 모두 여기에 해당한다. 그 커널은 Execution을 취소해서 정리한다.
- 커널의 마지막 활동(Jupyter `GET /api/kernels/{kernel_id}`의 `last_activity`)이 15분(900초) 안이면 지우지 않는다. `409 RUNTIME_KERNEL_DELETE_CONFLICT`, `type`은 `kernel_recently_active`, `context`에 `idle_seconds`와 `min_idle_seconds`. 커널은 만들어질 때 활동 상태로 시작하고 남겨진 커널은 아무도 쓰지 않으므로, Worker가 아직 기록하는 중일 수 있는 커널은 여기서 걸러진다. Jupyter 기본 API만 쓰므로 Jupyter 이미지를 다시 빌드할 필요가 없다.
- 커널 상태를 먼저 읽고 DB를 나중에 확인한다. 둘 중 하나라도 실패하면 지우지 않고 `503 RUNTIME_UNAVAILABLE` 또는 DB 오류를 준다.
- Target에 이미 없는 커널은 지우지 않고 `204`. 여러 번 불러도 결과가 같다.
- Target의 Jupyter를 이 Executor만 써야 한다. 다른 Executor나 사람이 같은 Jupyter에 띄운 커널은 남겨진 커널로 보인다.
- 지운 커널은 Executor 로그에 `Deleted unheld Runtime kernel`로 남는다.

<a id="ep-8"></a>
### 8. 즉시 점검

`POST /api/v1/runtime-targets/{target_id}/probe`

- MCP: `runtime_target_probe`
- 원문 요약: Probe one enabled Runtime Target now

사용 중(enabled)인 Target을 지금 점검하고 건강 상태·지원 프로파일을 저장한다. 바디 없음.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `target_id` | O | string(uuid) |  | Runtime Target ID |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`RuntimeTargetResponse`](#schema-runtimetargetresponse) | 성공 |
| 400 | [`ErrorResponse`](#schema-errorresponse) | 식별·설정 오류 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-9"></a>
### 9. 배정 중지(drain)

`POST /api/v1/runtime-targets/{target_id}/drain`

- MCP: `runtime_target_set_state (desired_state=DRAINING)`
- 원문 요약: Stop assigning new work while current work finishes

새 작업 배정을 멈추고 진행 중 작업은 끝까지 둔다. `state.drain_complete`로 비었는지 확인한다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `target_id` | O | string(uuid) |  | Runtime Target ID |

**요청 바디** `RuntimeTargetMutationRequest` (모르는 필드는 거부)

| 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |

예:

```json
{
  "idempotency_key": "drain-jupyter-01-1"
}
```

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`RuntimeTargetResponse`](#schema-runtimetargetresponse) | 성공 |
| 400 | [`ErrorResponse`](#schema-errorresponse) | 식별·설정 오류 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-10"></a>
### 10. 배정 재개

`POST /api/v1/runtime-targets/{target_id}/activate`

- MCP: `runtime_target_set_state (desired_state=ACTIVE)`
- 원문 요약: Enable, probe, and return a healthy target to active scheduling

사용으로 바꾸고 점검한 뒤 건강하면 `ACTIVE`로 돌려 배정을 재개한다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `target_id` | O | string(uuid) |  | Runtime Target ID |

**요청 바디** `RuntimeTargetMutationRequest` (모르는 필드는 거부)

| 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |

예:

```json
{
  "idempotency_key": "activate-jupyter-01-1"
}
```

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`RuntimeTargetResponse`](#schema-runtimetargetresponse) | 성공 |
| 400 | [`ErrorResponse`](#schema-errorresponse) | 식별·설정 오류 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-11"></a>
### 11. 비활성화

`POST /api/v1/runtime-targets/{target_id}/disable`

- MCP: `runtime_target_disable`
- 원문 요약: Disable a target while preserving execution history

Target을 끄고 `OFFLINE`으로 표시한다. 조회는 계속 되고 실행 이력은 보존된다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `target_id` | O | string(uuid) |  | Runtime Target ID |

**요청 바디** `RuntimeTargetMutationRequest` (모르는 필드는 거부)

| 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |

예:

```json
{
  "idempotency_key": "disable-jupyter-01-1"
}
```

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`RuntimeTargetResponse`](#schema-runtimetargetresponse) | 성공 |
| 400 | [`ErrorResponse`](#schema-errorresponse) | 식별·설정 오류 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

## Executor 유지보수

배포·점검 때 Executor 전체의 새 실행 수용을 멈추고, 진행 중 실행을 일괄 중지한다. admin key 전용.

<a id="ep-12"></a>
### 12. 유지보수 상태

`GET /api/v1/maintenance`

- MCP: 없음(REST 전용)
- 원문 요약: Get Executor-wide admission and shutdown readiness

Executor 전체 수용 상태, 작업량, 정리 상태, 진행 중 Run, 종료 안전 여부(`safe_to_shutdown`)를 준다.

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutorMaintenanceResponse`](#schema-executormaintenanceresponse) | 성공 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-13"></a>
### 13. 전체 수용 중지

`POST /api/v1/maintenance/drain`

- MCP: 없음(REST 전용)
- 원문 요약: Stop admission of new Executions across all Workers

모든 Worker가 새 Execution을 받지 않게 한다. 진행 중 실행은 계속된다.

**요청 바디** `ExecutorMaintenanceMutationRequest` (모르는 필드는 거부)

| 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |

예:

```json
{
  "idempotency_key": "deploy-20261108-drain"
}
```

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutorMaintenanceResponse`](#schema-executormaintenanceresponse) | 성공 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-14"></a>
### 14. 전체 수용 재개

`POST /api/v1/maintenance/activate`

- MCP: 없음(REST 전용)
- 원문 요약: Resume admission of queued Executions across all Workers

대기 중인 Execution의 수용을 다시 시작한다.

**요청 바디** `ExecutorMaintenanceMutationRequest` (모르는 필드는 거부)

| 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |

예:

```json
{
  "idempotency_key": "deploy-20261108-activate"
}
```

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`ExecutorMaintenanceResponse`](#schema-executormaintenanceresponse) | 성공 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

<a id="ep-15"></a>
### 15. 일괄 중지 Run 생성

`POST /api/v1/maintenance/runs`

- MCP: 없음(REST 전용)
- 원문 요약: Create a durable Run that stops active Executions

지금 진행 중인 Execution을 잡아 차례로 중지하는 영속 Run을 만든다. 응답 `202`.

**요청 바디** `MaintenanceRunCreateRequest` (모르는 필드는 거부)

| 필드 | 타입 | 필수 | 제약 | 설명 |
| --- | --- | :---: | --- | --- |
| `idempotency_key` | string | O | 길이≥1, 길이≤255 | 멱등성 키(1~255자). 같은 키·같은 요청은 첫 결과를 돌려주고, 같은 키로 다른 요청이면 `409 IDEMPOTENCY_CONFLICT` |
| `action` | `STOP_ACTIVE_EXECUTIONS` | O |  | `STOP_ACTIVE_EXECUTIONS`: 진행 중 Execution 일괄 중지 |

예:

```json
{
  "idempotency_key": "deploy-20261108-stop",
  "action": "STOP_ACTIVE_EXECUTIONS"
}
```

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 202 | [`MaintenanceRunResponse`](#schema-maintenancerunresponse) | 접수됨(비동기 처리) |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- 이미 진행 중인 Run이 있으면 `409 MAINTENANCE_RUN_CONFLICT`.

<a id="ep-16"></a>
### 16. Run 상세

`GET /api/v1/maintenance/runs/{run_id}`

- MCP: 없음(REST 전용)
- 원문 요약: Get one Maintenance Run and its target counts

Run 하나와 대상 집계.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `run_id` | O | string(uuid) |  | Maintenance Run ID |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`MaintenanceRunResponse`](#schema-maintenancerunresponse) | 성공 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |

<a id="ep-17"></a>
### 17. Run 대상 목록

`GET /api/v1/maintenance/runs/{run_id}/targets`

- MCP: 없음(REST 전용)
- 원문 요약: List the Execution targets captured by a Maintenance Run

Run이 잡은 Execution별 중지 진행.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `run_id` | O | string(uuid) |  | Maintenance Run ID |
| query | `cursor` |  | string \| null | 길이≤2048 | 이전 응답의 `next_cursor`를 그대로 |
| query | `limit` |  | integer | ≥1, ≤200, 기본 `100` | 페이지 크기 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`MaintenanceRunTargetPageResponse`](#schema-maintenanceruntargetpageresponse) | 성공 |
| 409 | [`ErrorResponse`](#schema-errorresponse) | 현재 상태·버전·멱등성과 충돌 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |

## 이벤트 Stream 조회

Redis가 지금 들고 있는 이벤트 Stream과 소비 상태를 읽는다. admin key 전용이고 읽기만 한다(`EXISTS`, `XINFO`, `XREVRANGE`, `XRANGE`, `XPENDING`). Executor가 쓰는 Redis 연결을 그대로 쓰므로 Sentinel과 standalone에서 똑같이 동작한다. Executor가 무엇을 발행했는지는 [이벤트 이력](01-executions.md)을, Stream에 무엇이 있고 소비자가 어디까지 읽었는지는 이 API를 본다.

<a id="ep-18"></a>
### 18. Stream 전체 요약

`GET /api/v1/event-streams`

- MCP: 없음(REST 전용)
- 원문 요약: Show the Redis connection and every tenant's event Stream

Redis 연결(모드, 지금 붙어 있는 주소), 보관 설정, 모든 tenant의 Stream과 consumer group을 한 번에 준다.

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`EventStreamOverviewResponse`](#schema-eventstreamoverviewresponse) | 성공 |
| 503 | [`ErrorResponse`](#schema-errorresponse) | Redis 또는 Runtime Target에 닿지 못함. 나중에 다시 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- tenant 목록은 Redis `SCAN`이 아니라 PostgreSQL에서 가져온다. Redis에 Stream이 없는 tenant는 `exists=false`로 나온다.
- Sentinel에서는 `connection.address`가 현재 마스터다. failover 뒤에는 새 마스터 주소가 나온다.
- Redis 장애·failover 중·ACL 거부는 `503 REDIS_UNAVAILABLE`.

<a id="ep-19"></a>
### 19. Stream 상세

`GET /api/v1/event-streams/{tenant_id}`

- MCP: 없음(REST 전용)
- 원문 요약: Show one tenant's event Stream and its consumer groups

tenant 하나의 Stream 길이, 첫·마지막 항목, group별 lag·pending·consumer를 준다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `tenant_id` | O | string | `^[a-z][a-z0-9-]{1,62}[a-z0-9]$` | Stream의 tenant |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`EventStreamResponse`](#schema-eventstreamresponse) | 성공 |
| 503 | [`ErrorResponse`](#schema-errorresponse) | Redis 또는 Runtime Target에 닿지 못함. 나중에 다시 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- Stream이 없으면 `404 EVENT_STREAM_NOT_FOUND`.
- `lag`은 group이 아직 받지 않은 항목 수, `pending`은 받았지만 ACK하지 않은 항목 수다.

<a id="ep-20"></a>
### 20. Stream 항목

`GET /api/v1/event-streams/{tenant_id}/entries`

- MCP: 없음(REST 전용)
- 원문 요약: List a Stream's entries, newest first

Stream 항목을 최신순으로 준다. `next_before`를 `before`로 넘기면 더 오래된 항목으로 이어진다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `tenant_id` | O | string | `^[a-z][a-z0-9-]{1,62}[a-z0-9]$` | Stream의 tenant |
| query | `limit` |  | integer | ≥1, ≤200, 기본 `50` | 페이지 크기 |
| query | `before` |  | string \| null | `^\d{1,20}(-\d{1,20})?$` | 이 Stream ID보다 오래된 항목부터. 이전 응답의 `next_before`를 그대로 |
| query | `execution_id` |  | string \| null | 길이≤64 | Execution ID |
| query | `event_type` |  | string \| null | 길이≤128 | 이 이벤트 유형만 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`EventStreamEntryPageResponse`](#schema-eventstreamentrypageresponse) | 성공 |
| 503 | [`ErrorResponse`](#schema-errorresponse) | Redis 또는 Runtime Target에 닿지 못함. 나중에 다시 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- `execution_id`나 `event_type`으로 거르면 한 요청이 최대 1000건만 읽는다. `limit`보다 적게 오고 `next_before`가 있으면 이어서 부른다. `next_before=null`이면 Stream의 처음까지 읽은 것이다.
- 항목은 저장된 그대로다. `payload`는 JSON으로 풀어 주고 이벤트 계약 검증은 하지 않는다.
- 보관 기간(`retention_seconds`)이 지난 항목은 Redis에 없다. 그때는 [이벤트 이력](01-executions.md)을 본다.

<a id="ep-21"></a>
### 21. pending 항목

`GET /api/v1/event-streams/{tenant_id}/groups/{group}/pending`

- MCP: 없음(REST 전용)
- 원문 요약: List entries a consumer group has not acknowledged

consumer group이 받았지만 ACK하지 않은 항목을 오래된 순으로 준다. 어떤 이벤트가 걸려 있는지 알 수 있게 `execution_id`, `event_type`, `event_sequence`를 붙인다.

**파라미터**

| 위치 | 이름 | 필수 | 타입 | 제약 | 설명 |
| --- | --- | :---: | --- | --- | --- |
| path | `tenant_id` | O | string | `^[a-z][a-z0-9-]{1,62}[a-z0-9]$` | Stream의 tenant |
| path | `group` | O | string | 길이≥1, 길이≤255 | consumer group 이름 |
| query | `limit` |  | integer | ≥1, ≤100, 기본 `50` | 페이지 크기 |
| query | `consumer` |  | string \| null | 길이≤255 | 이 consumer의 항목만 |
| query | `min_idle_ms` |  | integer | ≥0, 기본 `0` | 마지막 전달 뒤 이 시간(ms) 이상 지난 항목만 |

**응답**

| 상태 | 바디 | 의미 |
| --- | --- | --- |
| 200 | [`PendingEntryPageResponse`](#schema-pendingentrypageresponse) | 성공 |
| 503 | [`ErrorResponse`](#schema-errorresponse) | Redis 또는 Runtime Target에 닿지 못함. 나중에 다시 |
| 404 | [`ErrorResponse`](#schema-errorresponse) | 대상이 없거나 호출자에게 보이지 않음 |
| 422 | [`ErrorResponse`](#schema-errorresponse) | 요청 형식·규칙 오류 |

**주의**

- Stream이나 group이 없으면 `404 EVENT_STREAM_NOT_FOUND`.
- `idle_ms`가 크고 `delivery_count`가 늘어나는 항목은 소비자가 처리에 실패하고 있다는 뜻이다.
- 보관 정리로 이미 지워진 항목은 이벤트 필드 세 개가 `null`이다.

## 응답 스키마

응답에 나오는 모든 객체다. 타입 링크를 따라가면 하위 객체로 간다. `null` 가능 필드도 키는 항상 있다(필수 O).

<a id="schema-runtimetargetpageresponse"></a>
#### `RuntimeTargetPageResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `next_cursor` | string \| null | O | 다음 페이지 커서. 그대로 `cursor`로 보낸다. 마지막 페이지면 `null` |
| `has_more` | boolean | O | 다음 페이지가 있는지 |
| `items` | array<[RuntimeTargetResponse](#schema-runtimetargetresponse)> | O | 이번 페이지 항목 |

<a id="schema-errorresponse"></a>
#### `ErrorResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `error` | [ErrorDetail](#schema-errordetail) | O | 오류 |

<a id="schema-runtimetargetresponse"></a>
#### `RuntimeTargetResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `target_id` | string(uuid) | O | Runtime Target ID |
| `name` | string | O | Target 이름 |
| `runtime` | [RuntimeTargetRuntime](#schema-runtimetargetruntime) | O | Runtime 정보 |
| `state` | [RuntimeTargetState](#schema-runtimetargetstate) | O | 상태 |
| `capacity` | [RuntimeTargetCapacity](#schema-runtimetargetcapacity) | O | 용량 |
| `health` | [RuntimeTargetHealth](#schema-runtimetargethealth) | O | 점검 결과 |
| `resources` | [RuntimeTargetResources](#schema-runtimetargetresources) | O | cgroup v2로 관찰한 자원 |

<a id="schema-runtimepoolpageresponse"></a>
#### `RuntimePoolPageResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `items` | array<[RuntimePoolResponse](#schema-runtimepoolresponse)> | O | 이번 페이지 항목 |

<a id="schema-runtimetargetpurgeresponse"></a>
#### `RuntimeTargetPurgeResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `target_id` | string(uuid) | O | Runtime Target ID |
| `name` | string | O | 삭제된 Target 이름 |

<a id="schema-runtimetargetkernelsresponse"></a>
#### `RuntimeTargetKernelsResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `target_id` | string(uuid) | O | Runtime Target ID |
| `observed_at` | string(date-time) | O | Target에서 읽은 시각(UTC) |
| `items` | array<[TargetKernelResponse](#schema-targetkernelresponse)> | O | 지금 도는 커널 |

<a id="schema-executormaintenanceresponse"></a>
#### `ExecutorMaintenanceResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `admission` | [ExecutorAdmissionResponse](#schema-executoradmissionresponse) | O | 수용 상태 |
| `workload` | [ExecutorWorkloadResponse](#schema-executorworkloadresponse) | O | 작업량 |
| `cleanup` | [ExecutorCleanupResponse](#schema-executorcleanupresponse) | O | 정리 상태 |
| `active_run` | [ActiveMaintenanceRunResponse](#schema-activemaintenancerunresponse) \| null | O | 진행 중 Maintenance Run 또는 `null` |
| `safe_to_shutdown` | boolean | O | 지금 종료해도 안전한지 |

<a id="schema-maintenancerunresponse"></a>
#### `MaintenanceRunResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `maintenance_run_id` | string(uuid) | O | Maintenance Run ID |
| `action` | `STOP_ACTIVE_EXECUTIONS` | O | Run 동작 |
| `status` | `REQUESTED` \| `RUNNING` \| `SUCCEEDED` \| `FAILED` | O | `REQUESTED`/`RUNNING`/`SUCCEEDED`/`FAILED` |
| `targets` | [MaintenanceRunTargetCountsResponse](#schema-maintenanceruntargetcountsresponse) | O | 대상 Execution 집계 |
| `error_message` | string \| null | O | 실패 메시지 또는 `null` |
| `started_at` | string(date-time) \| null | O | 시작 시각 |
| `finished_at` | string(date-time) \| null | O | 종료 시각. 진행 중이면 `null` |

<a id="schema-maintenanceruntargetpageresponse"></a>
#### `MaintenanceRunTargetPageResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `items` | array<[MaintenanceRunTargetResponse](#schema-maintenanceruntargetresponse)> | O | 이번 페이지 항목 |
| `next_cursor` | string \| null | O | 다음 페이지 커서. 그대로 `cursor`로 보낸다. 마지막 페이지면 `null` |
| `has_more` | boolean | O | 다음 페이지가 있는지 |

<a id="schema-eventstreamoverviewresponse"></a>
#### `EventStreamOverviewResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `connection` | [RedisConnectionResponse](#schema-redisconnectionresponse) | O | Redis 연결 |
| `retention_seconds` | integer | O | 이보다 오래된 항목은 정리된다(`REDIS_EVENT_RETENTION_SECONDS`) |
| `max_length` | integer \| null | O | Stream별 대략적 상한(`REDIS_EVENT_STREAM_MAXLEN`). 없으면 `null` |
| `streams` | array<[EventStreamResponse](#schema-eventstreamresponse)> | O | tenant별 Stream(tenant 이름순) |

<a id="schema-eventstreamresponse"></a>
#### `EventStreamResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `tenant_id` | string | O | Stream의 tenant |
| `stream` | string | O | Redis 키 이름 |
| `exists` | boolean | O | Redis에 Stream이 있는지. `false`면 아래 값은 0·`null`·빈 목록 |
| `length` | integer | O | 지금 들어 있는 항목 수 |
| `entries_added` | integer | O | 지금까지 쓰인 항목 수(정리된 것 포함) |
| `first_entry` | [StreamPositionResponse](#schema-streampositionresponse) \| null | O | 가장 오래된 항목. 비어 있으면 `null` |
| `last_entry` | [StreamPositionResponse](#schema-streampositionresponse) \| null | O | 가장 최근 항목. 비어 있으면 `null` |
| `groups` | array<[ConsumerGroupResponse](#schema-consumergroupresponse)> | O | consumer group |

<a id="schema-eventstreamentrypageresponse"></a>
#### `EventStreamEntryPageResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `items` | array<[EventStreamEntryResponse](#schema-eventstreamentryresponse)> | O | 항목(최신순) |
| `next_before` | string \| null | O | 더 오래된 항목을 읽을 때 `before`로 보낸다. Stream의 처음까지 읽었으면 `null` |

<a id="schema-pendingentrypageresponse"></a>
#### `PendingEntryPageResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `group` | string | O | consumer group 이름 |
| `total_pending` | integer | O | group의 전체 pending 수(필터 적용 전) |
| `items` | array<[PendingEntryResponse](#schema-pendingentryresponse)> | O | pending 항목(오래된 순) |

<a id="schema-errordetail"></a>
#### `ErrorDetail`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `code` | string | O | 오류 코드(아래 표) |
| `message` | string | O | 사람이 읽는 메시지 |
| `details` | array<[ValidationIssue](#schema-validationissue)> \| null |  | 필드별 문제 목록 |

<a id="schema-runtimetargetruntime"></a>
#### `RuntimeTargetRuntime`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `type` | `JUPYTER` | O | `JUPYTER` |
| `pool` | `INTERACTIVE` \| `BATCH` | O | Pool |
| `connection_config` | object | O | 접속 설정(인증정보 제외) |
| `supported_profiles` | array<string> | O | probe로 확인한 지원 커널 프로파일 |

<a id="schema-runtimetargetstate"></a>
#### `RuntimeTargetState`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `status` | `ACTIVE` \| `DRAINING` \| `OFFLINE` | O | `ACTIVE`, `DRAINING`, `OFFLINE` |
| `enabled` | boolean | O | 사용 여부 |
| `accepting_new_executions` | boolean | O | 새 Execution을 받는지 |
| `drain_complete` | boolean | O | drain 중 진행 작업이 다 끝났는지 |

<a id="schema-runtimetargetcapacity"></a>
#### `RuntimeTargetCapacity`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `max_concurrent_executions` | integer | O | 동시 Execution 슬롯 |
| `active_execution_count` | integer | O | 진행 중 Execution 수 |
| `active_session_count` | integer \| null | O | Jupyter에서 관찰한 커널 세션 수 또는 `null` |
| `admission_used_count` | integer | O | 배정 판단에 쓰는 사용량. 세션 관찰값이 신선하면 예약 수와 관찰 세션 수 중 큰 값 |
| `available_capacity` | integer | O | 남은 슬롯 |
| `admission_blocked` | boolean | O | 배정이 막혔는지 |
| `session_count_observed_at` | string(date-time) \| null | O | 세션 수 관찰 시각 |
| `session_count_fresh` | boolean | O | 세션 관찰값이 신선한지 |

<a id="schema-runtimetargethealth"></a>
#### `RuntimeTargetHealth`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `last_check_at` | string(date-time) \| null | O | 마지막 점검 시각 |
| `last_error` | string \| null | O | 마지막 오류 또는 `null` |

<a id="schema-runtimetargetresources"></a>
#### `RuntimeTargetResources`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `observed_at` | string(date-time) \| null | O | 관찰 시각 |
| `last_check_at` | string(date-time) \| null | O | 마지막 점검 시각 |
| `last_error` | string \| null | O | 마지막 오류 또는 `null` |
| `fresh` | boolean | O | 관찰값이 신선한지 |
| `source` | string \| null | O | 측정 출처(`CGROUP_V2`) |
| `estimated` | boolean \| null | O | 추정값인지(항상 false) |
| `process_count` | integer \| null | O | 프로세스 수 |
| `pressure_score` | number \| null | O | 부하 점수: 슬롯 사용률·CPU·메모리 사용률 중 최댓값. 관찰값이 오래되면 `null` |
| `cpu` | [CpuResources](#schema-cpuresources) | O | CPU |
| `memory` | [MemoryResources](#schema-memoryresources) | O | 메모리 |
| `errors` | array<string> | O | 측정 오류 코드 |

<a id="schema-runtimepoolresponse"></a>
#### `RuntimePoolResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `runtime` | [RuntimePoolIdentity](#schema-runtimepoolidentity) | O | Runtime 정보 |
| `targets` | [RuntimePoolTargets](#schema-runtimepooltargets) | O | Target 수 |
| `capacity` | [RuntimePoolCapacity](#schema-runtimepoolcapacity) | O | 용량 |
| `state` | [RuntimePoolState](#schema-runtimepoolstate) | O | Pool 상태 |

<a id="schema-targetkernelresponse"></a>
#### `TargetKernelResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `kernel_id` | string | O | Jupyter 커널 ID |
| `execution_state` | string \| null | O | Jupyter가 아는 커널 상태(`busy`, `idle` 등). 모르면 `null` |
| `process_count` | integer \| null | O | 커널 프로세스와 그 자식 프로세스 수 |
| `memory_bytes` | integer \| null | O | 지금 메모리(RSS 합계, byte) |
| `cpu_seconds` | number \| null | O | 커널 시작부터 쓴 CPU 시간(초, 끝난 자식 포함) |
| `execution_id` | string(uuid) \| null | O | 이 커널을 쓰는 Execution. 없으면 `null`(남겨진 커널) |
| `tenant_id` | string \| null | O | 그 Execution의 tenant |
| `execution_status` | `QUEUED` \| `RUNNING` \| `WAITING_FOR_OPERATION` \| `FINALIZING` \| `CANCEL_REQUESTED` \| `CANCELLED` \| `SUCCEEDED` \| `FAILED` \| null | O | 그 Execution의 상태 |

<a id="schema-executoradmissionresponse"></a>
#### `ExecutorAdmissionResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `state` | `ACTIVE` \| `DRAINING` | O | `ACTIVE` 또는 `DRAINING` |
| `accepting_new_executions` | boolean | O | 새 Execution을 받는지 |
| `version` | integer | O | 수용 상태 버전 |

<a id="schema-executorworkloadresponse"></a>
#### `ExecutorWorkloadResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `queued_execution_count` | integer | O | 대기 Execution 수 |
| `active_execution_count` | integer | O | 진행 Execution 수 |
| `cancel_requested_count` | integer | O | 취소 요청 중 Execution 수 |

<a id="schema-executorcleanupresponse"></a>
#### `ExecutorCleanupResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `unresolved_cleanup_count` | integer | O | 정리 안 된 커널 수 |
| `active_runtime_session_count` | integer | O | 살아 있는 커널 세션 수 |

<a id="schema-activemaintenancerunresponse"></a>
#### `ActiveMaintenanceRunResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `maintenance_run_id` | string(uuid) | O | Maintenance Run ID |
| `action` | `STOP_ACTIVE_EXECUTIONS` | O | Run 동작 |
| `status` | `REQUESTED` \| `RUNNING` \| `SUCCEEDED` \| `FAILED` | O | Run 상태 |

<a id="schema-maintenanceruntargetcountsresponse"></a>
#### `MaintenanceRunTargetCountsResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `total` | integer | O | 전체 |
| `pending` | integer | O | 대기 |
| `stop_requested` | integer | O | 중지 요청됨 |
| `stopped` | integer | O | 중지됨 |
| `failed` | integer | O | 실패 |
| `remaining` | integer | O | 남은 수 |

<a id="schema-maintenanceruntargetresponse"></a>
#### `MaintenanceRunTargetResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `created_by_client` | string \| null | O | 만든 요청의 API key client 이름(`AUTH_CLIENTS[].name`) |
| `created_by` | string \| null | O | 만든 요청의 `X-User-Id`. Worker가 만든 행은 그 Execution의 마지막 행위자를 이어받는다 |
| `updated_by_client` | string \| null | O | 마지막으로 바꾼 요청의 client 이름 |
| `updated_by` | string \| null | O | 마지막으로 바꾼 요청의 `X-User-Id`(예: 취소 요청자) |
| `created_at` | string(date-time) | O | 생성 시각(UTC) |
| `updated_at` | string(date-time) | O | 마지막 변경 시각(UTC). 출력 시각이 아니다 |
| `target_id` | string(uuid) | O | Run 대상 행 ID |
| `maintenance_run_id` | string(uuid) | O | Maintenance Run ID |
| `execution_id` | string(uuid) | O | Execution ID |
| `selected_execution_status` | `QUEUED` \| `RUNNING` \| `WAITING_FOR_OPERATION` \| `FINALIZING` \| `CANCEL_REQUESTED` \| `CANCELLED` \| `SUCCEEDED` \| `FAILED` | O | Run이 잡을 때의 Execution 상태 |
| `status` | `PENDING` \| `STOP_REQUESTED` \| `STOPPED` \| `FAILED` | O | `PENDING`/`STOP_REQUESTED`/`STOPPED`/`FAILED` |
| `error_message` | string \| null | O | 실패 메시지 또는 `null` |
| `stop_requested_at` | string(date-time) \| null | O | 중지 요청 시각 |
| `completed_at` | string(date-time) \| null | O | 완료 시각 |

<a id="schema-redisconnectionresponse"></a>
#### `RedisConnectionResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `mode` | `standalone` \| `sentinel` | O | `standalone` 또는 `sentinel`(`REDIS_MODE`) |
| `address` | string | O | Executor가 붙어 있는 host:port. Sentinel에서는 현재 마스터 |

<a id="schema-streampositionresponse"></a>
#### `StreamPositionResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `id` | string | O | Redis Stream 항목 ID |
| `at` | string(date-time) | O | Redis가 그 ID를 매긴 시각(UTC) |

<a id="schema-consumergroupresponse"></a>
#### `ConsumerGroupResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `name` | string | O | group 이름 |
| `pending` | integer | O | 받았지만 ACK하지 않은 항목 수 |
| `lag` | integer \| null | O | group이 아직 받지 않은 항목 수. Redis가 계산할 수 없으면 `null`(항목이 삭제된 뒤 등) |
| `entries_read` | integer \| null | O | group이 지금까지 읽은 항목 수 |
| `last_delivered` | [StreamPositionResponse](#schema-streampositionresponse) \| null | O | 마지막으로 전달된 항목. 아무것도 읽지 않았으면 `null` |
| `oldest_pending` | [StreamPositionResponse](#schema-streampositionresponse) \| null | O | 가장 오래된 pending 항목. 없으면 `null` |
| `consumers` | array<[ConsumerResponse](#schema-consumerresponse)> | O | group의 consumer |

<a id="schema-eventstreamentryresponse"></a>
#### `EventStreamEntryResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `id` | string | O | Redis Stream 항목 ID |
| `at` | string(date-time) | O | Redis에 들어간 시각(UTC) |
| `event_id` | string \| null | O | 이벤트 ID. 이 필드부터 `tenant_id`까지는 저장된 값 그대로이고, 항목에 없으면 `null` |
| `event_type` | string \| null | O | 이벤트 유형 |
| `schema_version` | string \| null | O | 스키마 버전(`1.0`) |
| `execution_id` | string \| null | O | Execution ID |
| `event_sequence` | integer \| null | O | Execution별 순번. 숫자가 아니면 `null` |
| `occurred_at` | string \| null | O | 발생 시각(저장된 문자열 그대로) |
| `tenant_id` | string \| null | O | 항목에 적힌 tenant |
| `payload` | any | O | JSON으로 푼 payload. JSON이 아니면 저장된 문자열 |

<a id="schema-pendingentryresponse"></a>
#### `PendingEntryResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `id` | string | O | Redis Stream 항목 ID |
| `at` | string(date-time) | O | Redis에 들어간 시각(UTC) |
| `consumer` | string | O | 항목을 받은 consumer |
| `idle_ms` | integer | O | 마지막 전달 뒤 지난 시간(ms) |
| `delivery_count` | integer | O | 전달된 횟수 |
| `execution_id` | string \| null | O | Execution ID. 항목이 이미 정리됐으면 아래 두 필드와 함께 `null` |
| `event_type` | string \| null | O | 이벤트 유형 |
| `event_sequence` | integer \| null | O | Execution별 순번 |

<a id="schema-validationissue"></a>
#### `ValidationIssue`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `location` | array<string \| integer> | O | `body`/`header`/`query`/`path` + 필드 경로 |
| `message` | string | O | 메시지 |
| `type` | string | O | 안정적인 snake_case 사유 |
| `context` | object \| null |  | 한도와 실제 값 등 근거 |

<a id="schema-cpuresources"></a>
#### `CpuResources`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `used_cores` | number \| null | O | 사용 코어 |
| `capacity_cores` | number \| null | O | 가용 코어 |
| `utilization` | number \| null | O | 사용률(0~1) |

<a id="schema-memoryresources"></a>
#### `MemoryResources`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `used_bytes` | integer \| null | O | 사용 바이트 |
| `capacity_bytes` | integer \| null | O | 가용 바이트 |
| `utilization` | number \| null | O | 사용률(0~1) |

<a id="schema-runtimepoolidentity"></a>
#### `RuntimePoolIdentity`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `type` | `JUPYTER` | O | Runtime 종류 |
| `pool` | `INTERACTIVE` \| `BATCH` | O | Pool |

<a id="schema-runtimepooltargets"></a>
#### `RuntimePoolTargets`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `total` | integer | O | 전체 |
| `enabled` | integer | O | 사용 중 |
| `active` | integer | O | ACTIVE |
| `draining` | integer | O | DRAINING |
| `offline` | integer | O | OFFLINE |

<a id="schema-runtimepoolcapacity"></a>
#### `RuntimePoolCapacity`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `configured` | integer | O | 설정된 슬롯 합 |
| `schedulable` | integer | O | 배정 가능한 Target의 슬롯 합 |
| `reserved_execution_count` | integer | O | 예약된 Execution 수 |
| `available` | integer | O | 남은 슬롯 |

<a id="schema-runtimepoolstate"></a>
#### `RuntimePoolState`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `accepting_new_executions` | boolean | O | 새 Execution을 받는지 |
| `saturated` | boolean | O | 포화 여부 |

<a id="schema-consumerresponse"></a>
#### `ConsumerResponse`

| 필드 | 타입 | 필수 | 설명 |
| --- | --- | :---: | --- |
| `name` | string | O | consumer 이름 |
| `pending` | integer | O | 이 consumer가 받고 ACK하지 않은 항목 수 |
| `idle_ms` | integer | O | 마지막으로 읽기를 시도한 뒤 지난 시간(ms) |
| `inactive_ms` | integer \| null | O | 마지막으로 실제 항목을 읽거나 claim한 뒤 지난 시간(ms) |
