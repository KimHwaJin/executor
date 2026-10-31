# 오류 응답

REST와 MCP 오류는 모두 같은 형식으로 응답한다. 요청 값에 문제가 있으면 어떤 값이
왜 거절됐는지, 그리고 한도와 실제 값이 무엇이었는지를 `details`로 알려 준다.

## 형식

```json
{
  "error": {
    "code": "INVALID_EXECUTION_SPEC",
    "message": "An Execution may hold at most 1000 Steps; it has 990 and this Operation adds 20.",
    "details": [
      {
        "location": ["body", "spec", "steps"],
        "message": "An Execution may hold at most 1000 Steps; it has 990 and this Operation adds 20.",
        "type": "execution_step_limit_exceeded",
        "context": {"limit": 1000, "existing": 990, "requested": 20, "actual": 1010}
      }
    ]
  }
}
```

| 필드 | 의미 |
|---|---|
| `code` | 오류 종류. 호출측 분기 기준 |
| `message` | 사람이 읽는 설명. 문구는 바뀔 수 있으니 분기에 쓰지 않는다 |
| `details[]` | 요청 값 문제일 때만 있다. 값 하나에 하나씩 |
| `details[].location` | 값의 위치. 첫 원소는 `body` 또는 `header`이고, 그 뒤는 요청의 필드 이름과 배열 순번이다 |
| `details[].type` | 바뀌지 않는 snake_case 사유. 분기에 쓴다 |
| `details[].context` | 사유의 근거 값. 한도(`limit`, `limit_bytes`, `limit_seconds`)와 실제 값(`actual`, `actual_bytes`, `actual_seconds`) 등. 없으면 필드가 빠진다 |

MCP Tool 오류는 본문이 `[CODE] message details=[...]`이다. `details=` 뒤는 위의 `details`와
같은 JSON 배열이다.

## 상태 코드 규칙

| 상태 | 언제 | code |
|---|---|---|
| 400 | 식별 헤더가 잘못됨 | `INVALID_IDENTITY` |
| 401 / 403 | 인증 실패 / 권한 없음 | `UNAUTHENTICATED` / `FORBIDDEN` |
| 404 | 대상이 없음 | `*_NOT_FOUND` |
| 409 | **요청은 맞지만 지금 상태에서는 할 수 없음**. 이미 종료됨, `WAITING_FOR_OPERATION`이 아님, version 불일치, 멱등 키 재사용 등 | `INVALID_STATE_TRANSITION`, `EXECUTION_VERSION_CONFLICT`, `IDEMPOTENCY_CONFLICT` 등 |
| 413 | 요청 본문이 `REQUEST_BODY_MAX_BYTES`(기본 30MB)를 넘음 | `REQUEST_BODY_TOO_LARGE` |
| 422 | 요청 형식이 틀림(필수 값 누락, 타입, 길이, 패턴) | `REQUEST_VALIDATION_ERROR` |
| 422 | **요청 값 자체가 규칙에 맞지 않음**. 개수·크기·시간 한도 초과, 소스 파일 문제, actor 규칙 위반 등 | `INVALID_EXECUTION_SPEC` |

요청 값을 고쳐야 통과하면 422, 기다리거나 최신 상태를 다시 읽으면 통과할 수 있으면 409다.
Execution 누적 Step 한도 초과도 요청을 줄여야 하므로 422다.
MULTI 후속 Operation은 상태·version을 먼저 확인한다. 오래된 version으로 보내면 Step 번호
오류(422)보다 version 충돌(409)이 먼저 나온다.

## `INVALID_EXECUTION_SPEC`의 `type`

위치는 제출(`POST /executions`) 기준이다. 후속 Operation(`POST /executions/{id}/operations`)은
`operation` 단계가 없다. 예를 들어 `["body", "operation", "spec", "steps"]`는
`["body", "spec", "steps"]`가 된다.

| type | location | context |
|---|---|---|
| `too_many_steps` | `body.operation.spec.steps` | `limit`, `actual` |
| `execution_step_limit_exceeded` | `body.spec.steps` | `limit`, `existing`, `requested`, `actual` |
| `inline_source_too_large` | `...steps[i].payload.source.content` | `limit_bytes`, `actual_bytes`, `sequence` |
| `path_source_too_large` | `...steps[i].payload.source.path` | `limit_bytes`, `actual_bytes`, `path`, `sequence` |
| `path_not_relative` / `path_outside_root` / `path_not_python` / `path_not_found` / `path_not_utf8` | `...steps[i].payload.source.path` | `path`, `sequence` |
| `sha256_mismatch` | `...steps[i].payload.source.sha256` | `path`, `sequence`, `expected`, `actual` |
| `source_blank` | `...steps[i].payload.source` | `sequence` |
| `steps_empty` | `...spec.steps` | 없음 |
| `sequence_not_contiguous` | `...steps[i].sequence` | `expected`, `actual` |
| `first_sequence_not_zero` | `body.operation.spec.steps[0].sequence` | `expected`, `actual` |
| `unsupported_schema_version` | `...spec.schema_version` | `expected`, `actual` |
| `exceeds_max_runtime` | `body.operation.operation_timeout_seconds` 또는 `...steps[i].step_timeout_seconds` | `limit_seconds`, `actual_seconds` |
| `not_allowed_for_single` / `required_for_multi` | `body.lifecycle.operation_wait_timeout_seconds` | `actual`(있을 때) |
| `multi_requires_interactive` | `body.trigger.type` | `expected`, `actual` |
| `actor_not_allowed_for_trigger` | `body.trigger.actor.type` | `trigger_type`, `allowed`, `actual` |
| `actor_user_mismatch` | `body.trigger.actor.id` | `expected`(X-User-Id), `actual` |
| `actor_incomplete` / `actor_id_blank` | `body.trigger.actor` / `body.actor` | 없음 |
| `session_without_project` / `reserved_value` | `header.X-Session-Id` / `header.X-Project-Id` | `actual`(있을 때) |

## `REQUEST_VALIDATION_ERROR`

형식 검증 오류도 같은 `details` 형식이다. `type`은 Pydantic 오류 유형(`missing`,
`string_too_long`, `greater_than_equal` 등)이고, `context`에 그 한도가 들어간다.
예를 들어 255자를 넘는 `idempotency_key`는 다음과 같다.

```json
{"location": ["body", "idempotency_key"], "message": "String should have at most 255 characters",
 "type": "string_too_long", "context": {"max_length": 255}}
```
