# Executor OpenTelemetry 제거

## 범위

Executor의 수동 Span, HTTP 추적 미들웨어, Trace context 저장/전달,
OTLP exporter와 설정, Compose Phoenix 서비스 정의 및 전용 smoke를 제거한다.
일반 서비스 로그(`logger.yml`), 오류 마스킹, Diagnostics, 실행 이력,
Lease fencing, 멱등성, Outbox, Redis consumer/ACK/DLQ는 유지한다.

공개 REST/MCP 요청·응답과 Redis 이벤트 스키마는 변경하지 않는다.
Jupyter 확장 및 테스트 Agent 코드는 변경하지 않는다.

## 의존성 예외

공식 MCP SDK가 `opentelemetry-api`를 요구하므로 이 전이 의존성은 남는다.
Executor의 직접 OpenTelemetry 의존성과 SDK/Exporter 패키지는 제거한다.
외부 SDK를 수정하거나 의존성 없이 강제 설치하지 않는다.

## DB 삭제 승인 대기

기존 DB의 추적 값 삭제는 별도 승인을 기다린다. 현재 head는 `0003` 그대로다.
Execution, ExecutionEvent, OutboxEvent의 도메인에는 추적 필드가 없고,
ORM에는 기존 스키마 유지를 위한 nullable deferred 컬럼만 남긴다.
일반 조회는 해당 컬럼을 로드하지 않으며 서비스가 값을 전달하지 않는다.
기존 추적 데이터, 업무 이력, DB 및 Redis는 초기화하지 않는다.

승인 후 별도 마이그레이션에서 3개 테이블의 `traceparent`, `tracestate`
컬럼만 삭제하고 ORM 선언과 readiness revision을 함께 갱신한다.
컬럼을 제거하는 배포는 구버전 Executor를 모두 중지한 후 진행해야 한다.
추적 컬럼 삭제 이후 구버전으로 복귀하려면 nullable 컬럼을 먼저 복구해야 한다.

## Redis 전환

새 Work 메시지에는 추적 필드를 넣지 않는다. 이전 프로세스가 발행한 메시지는
`WorkStreamEnvelope.from_redis_fields()`에서 추적 필드 2개만 버리고 검증한다.
다른 미지정 필드, 잘못된 payload/ID는 계속 거부한다.
Public 이벤트의 event_id, event_sequence 및 payload는 그대로다.

## 배포 및 운영

1. 새 이미지에 갱신된 lock을 포함해 빌드한다.
2. ConfigMap/Secret/.env에서 기존 `TRACING_ENABLED`, `OTEL_*` 설정을 제거한다.
3. 실제 실행 중인 프로세스는 기존 maintenance/drain 절차에 따라 교체한다.
4. 로그, `GET /api/v1/executions/{execution_id}/diagnostics`,
   DB 실행 상태, 이벤트로 확인한다.

Compose에서 Phoenix 정의를 제거했지만 이미 실행 중인 Phoenix 컨테이너나 볼륨,
외부 Phoenix 서비스 및 보관된 Trace 데이터는 이 작업에서 삭제하지 않는다.
더 이상 사용하지 않는 배포는 운영자가 별도 판단해 종료할 수 있다.

## 회귀 검증

- 제출 → Outbox 발행 → Worker dispatch (신규/이전 Work 메시지).
- 공개 이벤트 스키마와 순번, DB 기반 복구, SINGLE/MULTI 및 취소/재시도.
- 실행 헬퍼의 반환값, 원래 예외/취소 전달, 실패 로그와 비밀값 마스킹.
- Executor 소스의 OpenTelemetry import·설정 재유입 방지.
- 실제 PostgreSQL/Alembic과 Redis 검증은 격리 테스트 DB/Stream으로 수행한다.

### 이번 변경 검증 결과

- `uv run python scripts/quality_gate.py --integration` 통과:
  기본 테스트 539개 통과/4개 환경 의존 제외, Redis 10개 통과,
  격리 PostgreSQL 및 기존 마이그레이션 24개 통과.
- 이후 추가한 deferred 컬럼 조회 검증까지 포함하여
  `tests/test_telemetry_removal.py` 8개 통과.
- Ruff lint/format, ty, Compose config, `git diff --check` 통과.
- lock 동기화 후 실제 환경에서 `opentelemetry.sdk` 미설치 확인.
- 실제 Jupyter 다운로드 Docker E2E 및 Linux UID 전환 테스트는 이번 검증에서
  실행하지 않았다. Jupyter 이미지/코드와 실행 중인 서비스는 변경하지 않았다.
