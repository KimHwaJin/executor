# MULTI Execution 종료 API

## API

```http
POST /api/v1/executions/{execution_id}/finalize
```

더 추가할 Operation이 없는 MULTI Execution을 비동기로 종료한다. 이 API는 최종 리포트를
자동 생성하는 API가 아니며, 현재까지 실행된 Operation을 기준으로 Execution 상태를 닫는
명령이다.

## 호출 조건

- Execution의 `operation_mode`가 `MULTI`여야 한다.
- 현재 상태가 `WAITING_FOR_OPERATION`이어야 한다.
- `expected_version`이 현재 Execution `state.version`과 같아야 한다.

## Request Body

| 필드 | 필수 | 의미 |
|---|---:|---|
| `idempotency_key` | O | finalize 명령의 멱등성 키 |
| `expected_version` | O | 상태조회 또는 이벤트에서 확인한 현재 Execution version. 0 이상 |
| `actor.type` | O | 종료 요청 주체: `AGENT`, `USER`, `BATCH` |
| `actor.id` | O | 종료 요청 주체 식별자 |

```json
{
  "idempotency_key": "finalize-task-100",
  "expected_version": 6,
  "actor": {"type": "AGENT", "id": "analytics-agent"}
}
```

## Response: `202 Accepted`

| 필드 | 의미 |
|---|---|
| `execution_id` | 종료 대상 MULTI Execution ID |
| `operation` | finalize는 새 Operation을 만들지 않으므로 `null` |
| `state.status` | 요청 반영 직후 `FINALIZING` |
| `state.version` | finalize 요청으로 증가한 최신 버전 |
| `created_by_type`, `created_by` | 최초 Execution 생성 주체 |
| `updated_by_type`, `updated_by` | finalize 요청 주체 |
| `created_at`, `updated_at` | 생성 및 마지막 변경 시각 |

응답 `Location`은 Execution 상태조회 API를 가리킨다. Worker의 finalization이 끝나면
Execution은 `SUCCEEDED` 또는 `FAILED` terminal 상태가 되고 Redis terminal 이벤트가
발행된다.

`409`가 발생하면 다른 Agent 요청이 상태를 먼저 변경했을 수 있다. 최신 Execution을 다시
조회하여 상태와 version을 확인한 후, 아직 `WAITING_FOR_OPERATION`일 때만 새 멱등성 키와 최신
version으로 다시 요청한다.

