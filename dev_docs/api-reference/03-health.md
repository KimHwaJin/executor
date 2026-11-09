# 헬스체크 API 스펙

Kubernetes probe와 운영자용 엔드포인트 3개다. 기준 경로 없이 루트에 있고 OpenAPI 문서에는 나오지 않는다(`include_in_schema=False`).

| # | 경로 | 인증 | 용도 |
| ---: | --- | --- | --- |
| 1 | `GET /healthz` | 없음 | liveness: 프로세스가 응답하는지 |
| 2 | `GET /readyz` | 없음 | readiness: 트래픽을 받아도 되는지 |
| 3 | `GET /workerz` | admin key | 이 Pod의 Worker 내부 상태 |

## 1. `GET /healthz`

항상 `200`.

```json
{"status": "ok"}
```

## 2. `GET /readyz`

| 상태 | 조건 |
| --- | --- |
| 200 | 모든 check가 `true` |
| 503 | 하나라도 `false` |

```json
{"status": "ready", "checks": {"postgresql": true, "worker_accepting": true}}
```

| 필드 | 의미 |
| --- | --- |
| `status` | `ready` 또는 `not_ready` |
| `checks.postgresql` | DB에 접속되고 스키마 revision이 코드가 기대하는 값 이상. 롤링 업데이트 중 스키마가 앞서도 ready 유지 |
| `checks.worker_accepting` | Worker가 새 작업을 받는 중. `RUNTIME_ENABLED=true`일 때만 들어 있다 |

Redis는 check에 없다. Redis 장애는 Outbox가 재시도하므로 Pod를 서비스에서 빼지 않는다.

## 3. `GET /workerz`

admin key 필요(아니면 `403 FORBIDDEN`). 운영자가 Pod 하나의 Worker를 볼 때 쓴다.

```json
{
  "worker_id": "executor-7c9f…",
  "state": "ACCEPTING",
  "accepting_new_executions": true,
  "active_execution_count": 2,
  "redis_reachable": true,
  "startup_reconciliation": {
    "completed_at": "2026-11-08T03:00:05+00:00",
    "recovered_execution_count": 0,
    "runtime_cleanup_target_count": 0
  }
}
```

| 필드 | 의미 |
| --- | --- |
| `worker_id` | 이 Worker의 ID |
| `state` | `STARTING`(시작 중), `ACCEPTING`(작업 받는 중), `DRAINING`(종료 준비, 새 작업 안 받음), `STOPPED` |
| `accepting_new_executions` | 새 Execution을 받는지 |
| `active_execution_count` | 이 Worker가 지금 처리 중인 작업 수 |
| `redis_reachable` | Redis ping 결과(참고용. 이벤트 발행은 스스로 재시도) |
| `startup_reconciliation.completed_at` | 시작 시 정합성 복구가 끝난 시각 또는 `null` |
| `startup_reconciliation.recovered_execution_count` | 시작 시 복구한 Execution 수 |
| `startup_reconciliation.runtime_cleanup_target_count` | 시작 시 커널 정리 대상 Target 수 |
