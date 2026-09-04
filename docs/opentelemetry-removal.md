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

## 승인된 DB 추적 컬럼 삭제

사용자 승인에 따라 마이그레이션 `0004`가 executions, execution_events,
outbox_events의 `traceparent`, `tracestate` 컬럼 6개와 그 값만 제거한다.
ORM의 임시 deferred 선언도 제거하고 readiness revision을 `0004`로 갱신한다.
Execution·Step·Operation·이벤트·Outbox 업무 이력, 결과 파일, Redis는 초기화하지 않는다.
기존 배포를 업그레이드할 수 있도록 이전 마이그레이션 파일은 수정하지 않는다.

구버전 Executor를 drain하고 모두 중지한 뒤 `uv run alembic upgrade head`를
실행하고 새 이미지를 시작한다. 구버전은 삭제된 컬럼을 조회하므로 혼용하면 안 된다.
잠긴 테이블에서는 DDL이 대기할 수 있으므로 장시간 트랜잭션도 먼저 확인한다.

구버전 이미지로 돌아가려면 신규 프로세스를 중지하고
`uv run alembic downgrade 0003`으로 nullable 컬럼을 복구한 뒤 실행한다.
다운그레이드는 컬럼 구조만 복구하며, 의도적으로 삭제한 과거 추적 값은 복구하지 않는다.
필요한 과거 추적 데이터는 업그레이드 전에 별도 백업해야 한다.

## Redis 전환

새 Work 메시지에는 추적 필드를 넣지 않는다. 이전 프로세스가 발행한 메시지는
`WorkStreamEnvelope.from_redis_fields()`에서 추적 필드 2개만 버리고 검증한다.
다른 미지정 필드, 잘못된 payload/ID는 계속 거부한다.
Public 이벤트의 event_id, event_sequence 및 payload는 그대로다.

## 배포 및 운영

1. 새 이미지에 갱신된 lock을 포함해 빌드한다.
2. ConfigMap/Secret/.env에서 기존 `TRACING_ENABLED`, `OTEL_*` 설정을 제거한다.
3. 기존 maintenance/drain 절차로 구버전을 중지하고 `0004` 마이그레이션 후
   새 이미지를 실행한다. DB/Redis 전체 삭제나 `alembic stamp`는 하지 않는다.
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

### 추적 코드 제거 단계 검증 결과

- `uv run python scripts/quality_gate.py --integration` 통과:
  기본 테스트 539개 통과/4개 환경 의존 제외, Redis 10개 통과,
  격리 PostgreSQL 및 기존 마이그레이션 24개 통과.
- 이후 추가한 deferred 컬럼 조회 검증까지 포함하여
  `tests/test_telemetry_removal.py` 8개 통과.
- Ruff lint/format, ty, Compose config, `git diff --check` 통과.
- lock 동기화 후 실제 환경에서 `opentelemetry.sdk` 미설치 확인.
- 실제 Jupyter 다운로드 Docker E2E 및 Linux UID 전환 테스트는 이번 검증에서
  실행하지 않았다. Jupyter 이미지/코드와 실행 중인 서비스는 변경하지 않았다.

### 추적 컬럼 제거 및 로컬 적용 결과

- `0004` 추가 후 전체 품질 검사 통과: 기본 542개 통과/4개 환경 의존 제외,
  실제 Redis 10개 통과, 격리 PostgreSQL/마이그레이션 25개 통과.
- 새 테스트는 0003→0004의 Execution/Operation/Step/이벤트/Outbox 업무 데이터
  보존, 추적 컬럼 제거, downgrade 시 NULL 컬럼 복구, 재업그레이드를 검증한다.
- 로컬 `localhost:5432/executor`에 실제 적용: Execution 233건,
  ExecutionEvent 1,530건, OutboxEvent 1,809건의 추적 필드 제외 SHA-256이
  적용 전후 동일했다. Redis·Jupyter 파일은 삭제하지 않았다.
- Executor 이미지를 재빌드·재시작하고 admission ACTIVE, `/readyz` ready,
  Worker ACCEPTING을 확인했다. 기존 Jupyter 컨테이너는 교체하지 않았다.
- 추가 MCP 목록 조회/제출은 성공했으나 실제 실행은 환경 불일치로 검증 미완료:
  로컬 Jupyter는 `basic/ml`, 새 Executor 허용 프로필은 `default/3102311`.
  런타임이 `RUNTIME_PROFILE_MISMATCH`로 OFFLINE 처리되어 요청이 QUEUED였다.
  테스트 요청 `0ae61f99-b285-413a-ae4f-2a5dff35d325`는 취소했다.
  다음 로컬 실연동 테스트 전에 Jupyter 이미지를 현행 커널 구성으로 갱신해야 한다.
  `/readyz`는 런타임 풀의 실제 가용 커널까지 보장하지 않는다.
