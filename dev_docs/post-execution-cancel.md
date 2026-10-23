# Execution 취소 API

## API

```http
POST /api/v1/executions/{execution_id}/cancel
```

실행 중인 Execution에 비동기 취소를 요청한다. `202 Accepted`는 취소 요청이 저장됐다는
뜻이며, Runtime kernel/session 정리가 완료됐다는 뜻은 아니다.

## Request Body

| 필드 | 필수 | 의미 |
|---|---:|---|
| `idempotency_key` | O | 취소 호출 멱등성 키. 동일 Execution에 같은 키를 재사용하면 같은 결과를 반환하며 다른 Execution에 재사용하면 `409` |
| `reason` | X | 취소 사유. 최대 2,000자. 생략 시 `null` |
| `actor.type` | O | 취소 요청 주체: `AGENT`, `USER`, `BATCH` |
| `actor.id` | O | 취소 요청 주체 식별자 |

```json
{
  "idempotency_key": "cancel-execution-100",
  "reason": "사용자가 분석을 취소했습니다.",
  "actor": {"type": "USER", "id": "user-100"}
}
```

terminal 상태인 `SUCCEEDED`, `FAILED`, `CANCELLED`에는 취소를 요청할 수 없다. 이미
`CANCEL_REQUESTED`이면 현재 상태를 그대로 반환한다.

## Response: `202 Accepted`

| 필드 | 의미 |
|---|---|
| `execution_id` | 취소 대상 Execution ID |
| `operation` | 취소는 새 Operation을 만들지 않으므로 `null` |
| `state.status` | 일반적으로 `CANCEL_REQUESTED` |
| `state.version` | 취소 요청 반영 후 상태 버전 |
| `created_by_type`, `created_by` | 최초 Execution 생성 주체 |
| `updated_by_type`, `updated_by` | 취소 요청을 반영한 주체 |
| `created_at`, `updated_at` | 생성 및 마지막 변경 시각 |

응답 `Location`은 Execution 상태조회 API를 가리킨다. 이후
`execution.completed` 이벤트의 `payload.status`가 `CANCELLED` 또는 `FAILED`가 될 때까지
기다리고, timeout이면
`GET /api/v1/executions/{execution_id}`로 확인한다.

취소가 확정되면 실행 중이던 Step은 `CANCELLED`, 아직 시작하지 않은 Step은 `SKIPPED`가
된다. `execution.step_completed`는 실행 중이던 Step에만 발행된다. 실패로 끝날 때와 같은
규칙이다.

`CANCELLED`는 취소 상태가 확정됐다는 뜻이지, 원격 커널 정리가 반드시 성공했다는
뜻은 아니다. 상태조회 응답의 `recovery.runtime_session_cleanup_status`를 별도로
확인한다. `PENDING`/`FAILED`이면 정리 상태와 diagnostics를 확인해야 한다.
취소 요청 성공이나 terminal 이벤트 수신만으로 Runtime 서버를 제거해도 된다고
판단하지 않는다. 이미 등록된 노트북은 취소 후에도 다운로드할 수 있지만,
최신 출력 반영 여부는 `workspace.notebook_projection`으로 확인한다.
