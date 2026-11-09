# Executor 연동 가이드 (코딩 에이전트용, 한글판)

> 사람이 검토하는 용도의 한글판이다. 코딩 에이전트에게는 영어판 [agent-guide.md](agent-guide.md)를 준다.

> 이 파일 하나로 Executor를 호출하는 코드를 작성할 수 있게 정리했다. 필드 타입·필수 여부가 더 필요하면
> 같은 폴더의 `openapi.json`(코드에서 생성)을 기준으로 삼는다. 이 문서와 openapi.json이 다르면 openapi.json이 맞다.
> Kubernetes에 배포된 Executor를 클러스터 안의 다른 서비스에서 호출하는 경우를 기준으로 쓴다.

## 0. 한 줄 요약

Executor는 Python 코드를 Jupyter 커널에서 **비동기로** 실행하는 HTTP 서비스다. 실행을 **제출**하면 `202`와
`execution_id`를 바로 돌려주고, 호출자는 **상태를 폴링하거나 Redis 이벤트를 받아** 끝났는지 확인한 뒤 **결과 API**로 결과를 읽는다.

## 1. 반드시 지킬 규칙

1. 모든 호출에 `X-Api-Key` 헤더를 넣는다. 쓰기(POST)에는 `X-User-Id`도 넣는다. 받은 key에 기본 tenant가 없으면(`pr-batch` 등) 모든 호출에 `X-Tenant-Id`도 넣는다.
2. 바디에 문서에 없는 필드를 넣지 않는다. 모르는 필드는 `422`로 거부된다. 특히 `tenant_id`, `user_id`, `actor`, `context`를 바디에 넣지 않는다(헤더로 보낸다).
3. 모든 POST 명령에 `idempotency_key`를 넣는다. **같은 작업을 재시도할 때는 같은 키**, 새 작업에는 새 키를 쓴다(예: `f"{task_id}:submit"`). 같은 키로 다른 바디를 보내면 `409 IDEMPOTENCY_CONFLICT`.
4. `202`는 "접수"다. 실행 완료로 취급하지 않는다.
5. 끝났는지는 `state.status`가 `SUCCEEDED`, `FAILED`, `CANCELLED` 중 하나인지로 판단한다. `WAITING_FOR_OPERATION`은 MULTI에서 다음 지시를 기다리는 상태다(끝난 것 아님).
6. 목록의 `next_cursor`는 파싱·수정하지 말고 그대로 `cursor`로 넘긴다.
7. 관리자 API(`/runtime-targets`, `/runtime-pools`, `/maintenance`, `/workerz`)는 호출하지 않는다. admin key 전용이다.
8. 5xx(`500 INTERNAL_ERROR`, `503 DATABASE_UNAVAILABLE`)와 네트워크 오류만 지수 백오프로 재시도한다(명령은 같은 `idempotency_key`로). 4xx는 재시도하지 말고 원인을 고친다.

## 2. 접속 정보

| 항목 | 값 |
| --- | --- |
| Base URL | `{EXECUTOR_URL}/api/v1`. 같은 namespace면 `http://executor:8000/api/v1`, 다른 namespace면 `http://executor.<namespace>.svc.cluster.local:8000/api/v1`. 코드에 박지 말고 환경변수 `EXECUTOR_URL`로 받는다 |
| 인증 | `X-Api-Key: {API_KEY}`. 환경(dev·stg·prd)마다 key가 다르다. 환경변수 `EXECUTOR_API_KEY`로 받고 로그에 찍지 않는다 |
| 사용자 | `X-User-Id: {user}` — 소문자 `^[a-z0-9][a-z0-9._-]{0,63}$`. 쓰기 필수. 읽기에 넣으면 그 사용자 것만 보인다 |
| tenant | `X-Tenant-Id` — key에 기본 tenant가 없으면 필수, 있으면 다른 tenant를 대신할 때만. 형식 `^[a-z][a-z0-9-]{1,62}[a-z0-9]$` |
| 프로젝트·세션(선택) | `X-Project-Id`, `X-Session-Id` — **제출(`POST /executions`)에만** 넣는다. 그 Execution이 어디 속하는지로 저장된다. `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`, `X-Session-Id`는 `X-Project-Id`와 함께 |
| 바디 | `Content-Type: application/json`, 최대 30MB |
| 시각 | UTC ISO-8601 문자열 |

다른 사용자의 Execution은 존재해도 `404`로 보인다. 같은 `X-User-Id`로 제출·조회·명령을 한다.
프로젝트·세션·사용자로 목록을 거를 때는 헤더가 아니라 쿼리(`project_id`, `session_id`, `user_id`)를 쓴다.

## 3. 핵심 개념

| 용어 | 뜻 |
| --- | --- |
| Execution | 실행 한 건. `execution_id`가 대표 키 |
| Operation | Execution 안의 Step 묶음 한 번. SINGLE은 1개, MULTI는 여러 개 |
| Step | 코드 한 조각(Jupyter 셀 하나). `sequence`는 **Execution 전체** 기준 0부터 연속 |
| Attempt | Worker가 실행을 맡은 한 번(최초, retry, 장애 복구). 조회 키로 쓰지 않는다 |
| SINGLE | 제출한 Operation이 끝나면 Execution도 끝난다 |
| MULTI | Operation이 끝나면 `WAITING_FOR_OPERATION`. 같은 커널(변수 유지)에 다음 코드를 붙이거나 finalize로 끝낸다 |
| `state.version` | 상태가 바뀔 때마다 증가. MULTI 명령의 `expected_version` |

## 4. 흐름 A — SINGLE: 코드 한 번 실행하고 결과 받기

```text
POST /executions  →  202 {execution_id, state.status=QUEUED}
loop: GET /executions/{id}  →  state.status 가 terminal 이 될 때까지 (2s → 최대 10s 백오프)
GET /executions/{id}/result →  Step별 status, output_summary, error_message, result_ref
```

제출 바디:

```json
{
  "idempotency_key": "task-100:submit",
  "lifecycle": {"operation_mode": "SINGLE"},
  "trigger": {"type": "INTERACTIVE"},
  "runtime": {"type": "JUPYTER", "profile": "default"},
  "operation": {
    "operation_timeout_seconds": 600,
    "spec": {
      "schema_version": "1.0",
      "steps": [
        {"sequence": 0, "payload": {"type": "PYTHON_EXECUTE", "source": {"type": "INLINE", "content": "import pandas as pd\nprint(pd.__version__)"}}},
        {"sequence": 1, "payload": {"type": "PYTHON_EXECUTE", "source": {"type": "INLINE", "content": "print(1 + 1)"}}, "step_timeout_seconds": 120}
      ]
    }
  },
  "metadata": {"task_id": "task-100"}
}
```

- SINGLE에는 `lifecycle.operation_wait_timeout_seconds`를 **보내지 않는다**(보내면 `422`).
- `trigger.type`: 사용자 대화형은 `INTERACTIVE`, 배치는 `BATCH`.
- `runtime.profile`: 보통 `default`. 허용값은 배포 설정에 따른다(기본 `default`, `3102311`).
- Step은 한 Operation에 최대 100개(기본 설정). sequence는 0, 1, 2… 빈틈없이.
- `metadata` 키는 `^[a-z][a-z0-9_]{0,63}$`, 32개 이하, 4096 bytes 이하. 비밀값 금지.

## 5. 흐름 B — MULTI: 결과를 보고 다음 코드를 이어서 실행

```text
POST /executions (operation_mode=MULTI, operation_wait_timeout_seconds=600)
loop until status ∈ {WAITING_FOR_OPERATION, SUCCEEDED, FAILED, CANCELLED}
GET  /executions/{id}/operations/{operation_id}/result   ← 방금 Operation 결과
     ↓ 결과를 보고 판단
 ├ 더 실행: POST /executions/{id}/operations {expected_version, spec(sequence 이어서)}
 ├ 끝내기 : POST /executions/{id}/finalize   {expected_version}   ← 마지막 Operation이 성공했을 때만
 └ 그만두기: POST /executions/{id}/cancel
```

- `expected_version`은 방금 `GET /executions/{id}`의 `state.version`이다. 다르면 `409` → 다시 조회해서 최신 값으로.
- 다음 Operation의 `sequence`는 지금까지 Step 총수부터 시작한다(첫 Operation이 0~1이었다면 다음은 2부터). `GET /executions/{id}/steps`의 개수나 직전 Operation의 `sequence_range.last + 1`을 쓴다.
- 다음 Operation 바디에는 `lifecycle`, `trigger`, `runtime`, `operation` 래퍼가 **없다**. `{idempotency_key, expected_version, operation_timeout_seconds?, spec, metadata?}`만 보낸다.
- 마지막 Operation이 실패한 상태에서 finalize하면 `409`. 고친 코드로 Operation을 하나 더 성공시키거나 cancel한다.
- `operation_wait_timeout_seconds`(30 이상) 안에 다음 요청이 없으면 `FAILED`(`OPERATION_WAIT_TIMEOUT`)로 끝난다.

```json
{
  "idempotency_key": "task-100:op-2",
  "expected_version": 4,
  "spec": {
    "schema_version": "1.0",
    "steps": [{"sequence": 2, "payload": {"type": "PYTHON_EXECUTE", "source": {"type": "INLINE", "content": "df.describe()"}}}]
  }
}
```

## 6. 결과 읽기

`GET /executions/{id}/result` 응답 구조(핵심만):

```text
execution: {execution_id, state: {status, version, cancellation_reason, finalized_by}}
operations[]: {operation_id, operation_number, sequence_range{first,last}, result{status, error_message}, lifecycle, steps[]}
  steps[]: {step_id, sequence, lineage, lifecycle,
            result: {status, error_message, output_summary{output_count, output_types, stream_names, mime_types, has_image, image_count, has_error},
                     result_ref: {relative_path, checksum_sha256, size_bytes, complete, ...} | null}}
attempts[]: Attempt 요약
artifacts[]: Artifact 요약(노트북, 리포트 등)
```

- Step 성공 여부는 `steps[].result.status`(`SUCCEEDED`/`FAILED`/`SKIPPED`/`CANCELLED` 등)로 본다. 실패 메시지는 `error_message`.
- **출력 원문(stdout, 표, 이미지)은 이 응답에 없다.** 두 가지 방법 중 하나로 읽는다.
  1. 공유 PVC를 마운트한 Pod인 경우: 루트는 `/mnt/data/dtest`다. `/mnt/data/dtest/{result_ref.relative_path}`(`executor/executions/...`)의 `manifest.json`을 읽고 `checksum_sha256`을 검증한 뒤, `outputs[].representations[].relative_path` 파일을 읽는다. 경로 결합 시 절대경로·`..`를 막는다.
  2. 공유 PVC가 없는 경우: 노트북 API를 쓴다. `GET /executions/{id}/notebook?view=FULL&start_index=0&limit=20` 또는 `GET /executions/{id}/notebook/cells/{index}`. 노트북이 아직 없으면 `409 EXECUTION_NOTEBOOK_NOT_AVAILABLE`이니 잠시 후 다시.
- 파일(Artifact)은 `GET /executions/{id}/artifacts`로 목록을 보고 `GET /artifacts/{artifact_id}/content`로 내려받는다.

## 7. 실패·취소·재시도

| 상황 | 어떻게 |
| --- | --- |
| 실행 취소 | `POST /executions/{id}/cancel` `{"idempotency_key": "...", "reason": "..."}` → `CANCEL_REQUESTED` → 나중에 `CANCELLED`. 이미 끝났으면 `409` |
| SINGLE 실패 재시도 | `GET /executions/{id}`의 `retry.strategy`가 `NOT_RETRYABLE`이 아니면 `POST /executions/{id}/retry` `{"idempotency_key": "..."}`. 시작 위치는 Executor가 정한다 |
| MULTI 실패 | retry 불가. 고친 코드로 다음 Operation을 제출한다 |
| 실패 원인 | `GET /executions/{id}`의 `failure{type, message}`, 더 자세히는 `GET /executions/{id}/diagnostics` |

`failure.type` 값: `TOOL_ERROR`(사용자 코드 예외), `STEP_TIMEOUT`, `OPERATION_TIMEOUT`, `OPERATION_WAIT_TIMEOUT`, `EXECUTION_TIMEOUT`, `QUEUE_TIMEOUT`, `OUTPUT_LIMIT_EXCEEDED`, `RUNTIME_UNAVAILABLE`, `RUNTIME_SESSION_LOST`, `INFRASTRUCTURE_ERROR`, `WORKER_SHUTDOWN`, `LEASE_EXPIRED`, `COMPLETION_FAILED`, `INTERNAL_ERROR`.

## 8. 오류 처리

오류 바디는 항상 `{"error": {"code", "message", "details": [{"location", "message", "type", "context"}]}}`.
`code`로 분기한다.

| HTTP | 대표 code | 코드에서 할 일 |
| --- | --- | --- |
| 400 | `INVALID_IDENTITY` | 헤더 값 수정. 재시도 금지 |
| 401 / 403 | `UNAUTHENTICATED` / `FORBIDDEN` | key·권한 문제. 재시도 금지 |
| 404 | `EXECUTION_NOT_FOUND` 등 | id 또는 `X-User-Id`가 다름 |
| 409 | `EXECUTION_VERSION_CONFLICT`, `INVALID_STATE_TRANSITION` | `GET /executions/{id}`로 상태·버전을 다시 읽고 판단 |
| 409 | `IDEMPOTENCY_CONFLICT` | 같은 키로 다른 바디를 보냄. 키 생성 로직 수정 |
| 409 | `EXECUTION_NOTEBOOK_NOT_AVAILABLE` | 잠시 후 다시 |
| 413 | `REQUEST_BODY_TOO_LARGE` | 코드를 줄이거나 PATH source 사용 |
| 422 | `REQUEST_VALIDATION_ERROR`, `INVALID_EXECUTION_SPEC`, `UNSUPPORTED_RUNTIME_PROFILE`, `INVALID_CURSOR` | `details[].location`이 가리키는 값 수정 |
| 500 / 503 | `INTERNAL_ERROR` / `DATABASE_UNAVAILABLE` | 백오프 재시도(같은 `idempotency_key`) |

## 9. Python 클라이언트 예시

```python
import time
import uuid

import httpx

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


class ExecutorError(Exception):
    def __init__(self, status: int, body: dict):
        err = body.get("error", {})
        self.status, self.code = status, err.get("code")
        self.details = err.get("details", [])
        super().__init__(f"{status} {self.code}: {err.get('message')}")


class ExecutorClient:
    def __init__(self, base_url: str, api_key: str, user_id: str,
                 tenant_id: str | None = None):
        headers = {"X-Api-Key": api_key, "X-User-Id": user_id}
        if tenant_id:
            headers["X-Tenant-Id"] = tenant_id
        self._http = httpx.Client(base_url=f"{base_url}/api/v1", headers=headers, timeout=30)

    def _call(self, method: str, path: str, **kw) -> dict:
        for attempt in range(5):
            try:
                r = self._http.request(method, path, **kw)
            except httpx.TransportError:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if r.status_code >= 400:
                raise ExecutorError(r.status_code, r.json())
            return r.json()
        raise RuntimeError(f"{method} {path} failed after retries")

    # --- commands -------------------------------------------------------
    def submit(self, codes: list[str], *, multi: bool = False, key: str | None = None,
               trigger: str = "INTERACTIVE", profile: str = "default",
               operation_timeout: int | None = 600, wait_timeout: int = 600,
               metadata: dict | None = None,
               project_id: str | None = None, session_id: str | None = None) -> dict:
        lifecycle = {"operation_mode": "MULTI" if multi else "SINGLE"}
        if multi:
            lifecycle["operation_wait_timeout_seconds"] = wait_timeout
        operation = {"spec": _spec(codes, start=0)}
        if operation_timeout:
            operation["operation_timeout_seconds"] = operation_timeout
        body = {
            "idempotency_key": key or f"submit-{uuid.uuid4()}",
            "lifecycle": lifecycle,
            "trigger": {"type": trigger},
            "runtime": {"type": "JUPYTER", "profile": profile},
            "operation": operation,
        }
        if metadata:
            body["metadata"] = metadata
        scope = {}
        if project_id:
            scope["X-Project-Id"] = project_id
            if session_id:
                scope["X-Session-Id"] = session_id
        return self._call("POST", "/executions", json=body, headers=scope)

    def add_operation(self, execution_id: str, codes: list[str], *, key: str) -> dict:
        state = self.get(execution_id)
        steps = self._call("GET", f"/executions/{execution_id}/operations", params={"limit": 200})
        next_seq = max((op["sequence_range"]["last"] for op in steps["items"]), default=-1) + 1
        return self._call("POST", f"/executions/{execution_id}/operations", json={
            "idempotency_key": key,
            "expected_version": state["state"]["version"],
            "spec": _spec(codes, start=next_seq),
        })

    def finalize(self, execution_id: str, *, key: str) -> dict:
        version = self.get(execution_id)["state"]["version"]
        return self._call("POST", f"/executions/{execution_id}/finalize",
                          json={"idempotency_key": key, "expected_version": version})

    def cancel(self, execution_id: str, *, key: str, reason: str | None = None) -> dict:
        return self._call("POST", f"/executions/{execution_id}/cancel",
                          json={"idempotency_key": key, "reason": reason})

    def retry(self, execution_id: str, *, key: str) -> dict:
        return self._call("POST", f"/executions/{execution_id}/retry", json={"idempotency_key": key})

    # --- queries --------------------------------------------------------
    def get(self, execution_id: str) -> dict:
        return self._call("GET", f"/executions/{execution_id}")

    def list(self, *, status: str | None = None, project_id: str | None = None,
             session_id: str | None = None, limit: int = 100) -> list[dict]:
        params = {k: v for k, v in {"status": status, "project_id": project_id,
                                    "session_id": session_id, "limit": limit}.items() if v}
        items, cursor = [], None
        while True:
            page = self._call("GET", "/executions", params={**params, **({"cursor": cursor} if cursor else {})})
            items += page["items"]
            if not page["has_more"]:
                return items
            cursor = page["next_cursor"]

    def result(self, execution_id: str) -> dict:
        return self._call("GET", f"/executions/{execution_id}/result")

    def notebook(self, execution_id: str, start: int = 0, limit: int = 20) -> dict:
        return self._call("GET", f"/executions/{execution_id}/notebook",
                          params={"view": "FULL", "start_index": start, "limit": limit})

    def wait(self, execution_id: str, until: set[str] = TERMINAL, timeout: float = 3600) -> dict:
        delay, deadline = 2.0, time.monotonic() + timeout
        while True:
            execution = self.get(execution_id)
            if execution["state"]["status"] in until:
                return execution
            if time.monotonic() > deadline:
                raise TimeoutError(execution_id)
            time.sleep(delay)
            delay = min(delay * 1.5, 10.0)


def _spec(codes: list[str], start: int) -> dict:
    return {"schema_version": "1.0", "steps": [
        {"sequence": start + i,
         "payload": {"type": "PYTHON_EXECUTE", "source": {"type": "INLINE", "content": code}}}
        for i, code in enumerate(codes)
    ]}


# SINGLE
import os
client = ExecutorClient(os.environ["EXECUTOR_URL"], api_key=os.environ["EXECUTOR_API_KEY"],
                        user_id="user-100", tenant_id=os.environ.get("EXECUTOR_TENANT_ID"))
accepted = client.submit(["print('hello')"], key="task-100:submit",
                         project_id="proj-1", session_id="chat-42")
done = client.wait(accepted["execution_id"])
result = client.result(accepted["execution_id"])
for op in result["operations"]:
    for step in op["steps"]:
        print(step["sequence"], step["result"]["status"], step["result"]["error_message"])

# MULTI
accepted = client.submit(["import pandas as pd\ndf = pd.DataFrame({'a': [1, 2]})"], multi=True, key="task-200:submit")
eid = accepted["execution_id"]
client.wait(eid, until=TERMINAL | {"WAITING_FOR_OPERATION"})
client.add_operation(eid, ["print(df.sum())"], key="task-200:op-2")
client.wait(eid, until=TERMINAL | {"WAITING_FOR_OPERATION"})
client.finalize(eid, key="task-200:finalize")
client.wait(eid)
```

`add_operation`의 sequence 계산은 Operation 목록이 200개를 넘지 않는다고 가정한다. 넘을 수 있으면 `next_cursor`로 끝까지 읽는다.

## 10. 폴링 대신 이벤트(선택)

대량·저지연이 필요하면 Redis Stream `executor.events.{tenant_id}`를 구독한다. 이벤트는 "다시 조회하라"는 신호로만 쓴다.

- 중요한 이벤트: `execution.operation_completed`(MULTI는 여기서 다음 판단, `payload.continuation.expected_version` 제공), `execution.completed`(끝).
- 같은 `event_id`가 중복으로 올 수 있다. `event_sequence`가 건너뛰면 `GET /executions/{id}/events?after_sequence={last}`로 채운다.
- 상세 규칙은 [05-events.md](05-events.md). 처음 구현할 때는 폴링으로 시작해도 된다.

## 11. MCP로 붙는 경우 (dev 전용)

MCP는 **dev 배포에서만** 켜져 있고 stg·prd에서는 `/mcp`가 없다(404). 운영까지 갈 코드는 REST로 작성한다.
dev에서는 같은 서버 `POST /mcp`(Streamable HTTP, stateless)에 같은 헤더로 연결한다. `Host`·`Origin`이 Executor의 허용 목록에 있어야 한다. 도구 이름은 `execution_submit`,
`execution_get`, `execution_result_get`, `execution_operation_create`, `execution_finalize`, `execution_cancel` 등이고,
명령 도구는 `request` 객체 하나(REST 바디 + `execution_id`)를 받는다. 오류는 `[CODE] message details=[...]`. 전체 목록은 [04-mcp.md](04-mcp.md).

## 12. 하지 말 것

- `202`를 성공으로 처리하기.
- `execution_id` 대신 `attempt_id`로 상태를 추적하기.
- `next_cursor`를 만들거나 고치기, 페이지 중간에 필터 바꾸기.
- 재시도마다 새 `idempotency_key` 만들기(중복 실행이 생긴다).
- MULTI에서 버전을 캐시해 두고 재사용하기(명령 직전에 다시 읽는다).
- 이벤트 payload만 믿고 최종 판단하기(최종 리포트 전 `GET .../result`로 확인).
- 관리자 API 호출, 응답에 없는 필드 가정.

## 13. 참고 엔드포인트 요약

| 하고 싶은 일 | 호출 |
| --- | --- |
| 실행 제출 | `POST /executions` |
| 상태 확인 | `GET /executions/{id}` |
| 결과 | `GET /executions/{id}/result` |
| Operation 하나의 결과 | `GET /executions/{id}/operations/{operation_id}/result` |
| MULTI 다음 코드 | `POST /executions/{id}/operations` |
| MULTI 종료 | `POST /executions/{id}/finalize` |
| 취소 | `POST /executions/{id}/cancel` |
| SINGLE 재시도 | `POST /executions/{id}/retry` |
| 내 실행 목록 | `GET /executions?status=&project_id=&session_id=&limit=` (`X-User-Id`를 넣으면 내 것만) |
| 노트북 보기 | `GET /executions/{id}/notebook?view=FULL` |
| 파일 목록·다운로드 | `GET /executions/{id}/artifacts`, `GET /artifacts/{artifact_id}/content` |
| 텍스트 리포트 저장 | `POST /executions/{id}/artifacts` (`SUCCEEDED` Execution만, `type: "REPORT"`) |
| 실패 진단 | `GET /executions/{id}/diagnostics` |
