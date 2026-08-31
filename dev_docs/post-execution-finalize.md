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
- 마지막 Operation의 Step이 모두 성공해야 한다. 과거 실패 이력은 허용하지만
  마지막 실패를 finalize로 성공 처리할 수는 없다.

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

최종화는 공유 결과를 읽어 노트북을 저장하고 최종 노트북 Artifact 등록과 런타임
해제까지 확인한다. 이 처리에 실패하면 `COMPLETION_FAILED / NOT_RETRYABLE`이며,
이미 성공한 Step과 Operation은 실패로 소급 변경하지 않는다.

`409`이면 먼저 응답 사유를 확인한다. version 충돌은 최신 상태를 조회한 뒤 재판단한다.
마지막 Operation 실패가 사유라면 finalize를 반복하지 말고 보정 Operation을 성공시킨
뒤 finalize하거나 cancel한다. [필수 결과 완료 정책](../docs/required-result-completion.md) 참고.
