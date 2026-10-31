# Execution 재시도 API

## API

```http
POST /api/v1/executions/{execution_id}/retry
```

실패한 SINGLE Execution을 비동기로 재시도한다. 기존 Execution과 Operation을 유지하면서
재시도 가능한 Step을 다시 대기 상태로 만든다. 새 Execution을 생성하지 않는다.

## 호출 조건

- Execution 상태가 `FAILED`여야 한다.
- `operation_mode`가 `SINGLE`이어야 한다.
- `retry.strategy`가 `NOT_RETRYABLE`이면 호출할 수 없다.
- `FROM_FAILED_STEP`은 보존된 Runtime target/session과 보존 만료시간이 유효해야 한다.
- `FROM_START`는 버려진 Runtime session cleanup이 해결된 뒤 호출할 수 있다.
- MULTI Tool 실패는 retry 대신 수정한 다음 Operation을 제출하는 방식으로 처리한다.

`failure.type=COMPLETION_FAILED`는 필수 노트북/아티팩트 처리 또는 최종 런타임
정리가 실패한 경우다. 최초 노트북 Artifact 등록 단계, 즉 코드 실행 전에도 발생할
수 있으므로 이 오류만으로 모든 Step의 성공을 추론하지 않는다.
`retry.strategy=NOT_RETRYABLE`이며 retry는 409다. 이미 성공한 코드는 저장 문제를
고치기 위해 재실행하지 않는다. 존재하는 result_ref로 결과를 읽고 Step 상태와
diagnostics로 실패 단계를 확인한다. 참조가 아직 없을 수도 있다.
코드 실행 없는 후처리 복구 API는 제공하지 않는다.

## Request Body

| 필드 | 필수 | 의미 |
|---|---:|---|
| `idempotency_key` | O | 재시도 호출 멱등성 키. Execution 단위로 구분하며 다른 Execution에서는 별개 요청이다 |
| `actor.type` | O | 재시도 요청 주체: `AGENT`, `USER`, `BATCH` |
| `actor.id` | O | 재시도 요청 주체 식별자 |

```json
{
  "idempotency_key": "retry-execution-100-1",
  "actor": {"type": "AGENT", "id": "analytics-agent"}
}
```

재시도 전략과 시작 sequence는 Agent가 요청으로 정하지 않는다. 실패 유형과 Runtime 보존
상태를 기준으로 Executor가 이미 계산한 `retry.strategy`와 `retry.from_sequence`를 사용한다.

## Response: `202 Accepted`

| 필드 | 의미 |
|---|---|
| `execution_id` | 기존 Execution ID |
| `operation.operation_id` | 다시 대기열에 넣은 기존 활성 Operation ID |
| `operation.steps[].sequence` | 해당 Operation의 논리 Step sequence |
| `operation.steps[].step_id` | 기존 논리 Step ID |
| `state.status` | 재시도 접수 후 `QUEUED` |
| `state.version` | 재시도 요청 반영 후 상태 버전 |
| `created_by_type`, `created_by` | 최초 Execution 생성 주체 |
| `updated_by_type`, `updated_by` | 재시도를 요청한 주체 |
| `created_at`, `updated_at` | 생성 및 마지막 변경 시각 |

`operation.steps`에는 Operation의 논리 Step receipt가 들어가며, 실제 재실행 시작점은
`GET /api/v1/executions/{execution_id}`의 `retry.from_sequence`로 확인한다.

응답 `Location`은 Execution 상태조회 API를 가리킨다. retry 전용 이벤트는 없다.
재실행 Step의 started/completed, Operation completed, Execution completed를 받는다.
`execution.started`와 `execution.operation_started`는 다시 발행하지 않는다.
이벤트 대기가 timeout이면 상태조회 API로 복구한다.

retry 시 Operation 시작 시각과 Operation 시간 예산은 새로 시작한다. 재실행 Step의
시작·종료 시각도 초기화되지만, Execution 최초 시작 시각과 전체 만료 시각은
유지된다. 요청으로 timeout을 변경하는 옵션은 없다.
보존 커널 유지시간의 기본값은 1시간(`FAILED_SESSION_RETENTION_SECONDS`)이며
`FROM_FAILED_STEP`은 그 기한 안에 유효한 커널이 있어야 한다.
관련 경계는 [시간 제한 가이드](execution-timeouts.md)를 참고한다.
