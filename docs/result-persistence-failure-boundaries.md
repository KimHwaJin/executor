# 결과 영속화 장애 경계

## 목적

Runtime 코드 실행 뒤 결과 파일, PostgreSQL 상태, Agent 이벤트가 서로 다른
성공을 보고하지 않도록 하는 경계를 정의한다. 공개 REST/MCP 요청·응답과 Redis
이벤트 스키마는 변경하지 않는다.

정상 성공 순서는 다음과 같다.

1. 공유 결과 스토리지에서 Step 출력을 원자적으로 기록하고 `manifest.json`을
   `FINALIZED / complete=true`로 봉인한다.
2. 하나의 PostgreSQL 트랜잭션에서 현재 Step, StepAttempt, 결과 참조,
   `execution.step_completed` 이력과 Outbox 행을 함께 기록한다.
3. 이후 Operation/노트북/Artifact/Runtime 해제 완료 조건을 검사한다.
4. 커밋된 Outbox만 Redis Stream에 순서대로 발행한다.

파일과 PostgreSQL은 하나의 분산 트랜잭션이 아니다. 파일 봉인 뒤 DB 트랜잭션이
실패하면 파일은 고아 증거로 남을 수 있지만, canonical 결과 참조나 성공 이벤트로
노출하지 않는다.

## 이번 보완

코드와 결과 봉인이 성공한 뒤 Step 결과 참조·이벤트 DB 트랜잭션이 실패하는 경우를
일반 내부 오류로 분류하지 않는다.

- Execution과 Attempt는 `COMPLETION_FAILED`로 종료한다.
- 재시도 전략은 `NOT_RETRYABLE`이다. 저장 후처리 실패를 복구하기 위해 성공한
  코드를 다시 실행하지 않는다.
- 실행 중이던 Step은 `FAILED`, 후속 Step은 `SKIPPED`가 된다.
- 롤백된 Step에는 `result_ref`가 없고 성공 Step 이벤트도 없다.
- 봉인된 고아 파일은 현재 Attempt·fencing 디렉터리에 남지만 DB에서 가리키지 않는다.
- 진단 API에는 `COMPLETION_FAILED / RESULT_REFERENCE_PERSIST`가 기록되며 원래 DB·OS
  오류는 cause chain으로 보존한다.
- Lease 소유권 상실은 완료 실패로 감싸지 않고 기존 fencing 경로를 그대로 따른다.

## 파일 장애

공유 결과 파일은 임시 파일 쓰기, file `fsync`, 원자적 `replace`, directory `fsync`
순으로 저장한다. 최종 파일을 공개하기 전 `fsync` 또는 `replace`가 ENOSPC·권한
오류로 실패하면 임시 파일을 제거하고 최종 파일을 만들지 않는다. Worker의 기존
결과 저장 실패 경로는 Step과 Execution을 성공으로 표시하지 않으며, 만들지 못한
manifest의 참조를 DB나 이벤트에 넣지 않는다.

`replace` 뒤 directory `fsync`처럼 호출자는 실패를 받았지만 파일이 보일 수 있는
경계에서도 DB 성공 참조가 없으므로 해당 파일은 canonical 결과가 아니다.

## Redis 장애

Redis는 결과 상태 원본이 아니다. 도메인 이벤트와 Outbox는 Step 상태·결과 참조와
같은 PostgreSQL 트랜잭션에 커밋된다. Redis 발행이 실패하거나 지연되면 PostgreSQL
성공 상태는 유지되고 Outbox가 재시도한다. 같은 Execution의 앞선 event sequence가
발행되지 않으면 후속 event sequence도 발행하지 않는다.

Agent는 Redis 지연을 Execution 실패로 해석하지 않는다. 누락·재연결 시
`GET /api/v1/executions/{execution_id}/events`의 durable history와
`event_sequence`로 복구한다.

## 검증

- `tests/test_required_result_completion.py`
  - SINGLE/MULTI에서 결과 파일 봉인 후 Step 결과·이벤트 트랜잭션 실패 주입
  - `COMPLETION_FAILED / NOT_RETRYABLE`, 성공 코드 1회 실행, 성공 이벤트 없음,
    공개 결과 참조 없음, 봉인 고아 파일 유지 검증
  - 진단 phase와 원래 `OSError` cause 보존 검증
- `tests/test_result_storage.py`
  - file `fsync` ENOSPC와 `replace` 권한 오류에서 최종 파일·임시 파일 비공개 검증
- `tests/test_runtime_failure_evidence.py`
  - 결과 준비·append·finalize·abort 권한 오류와 Runtime/cleanup 복합 실패 검증
- `tests/test_events.py`
  - Redis 발행 실패 후 선행 event sequence 우선 재시도와 순서 보존 검증
- `scripts/executor_redis_outage_smoke.py`
  - 실제 Redis pause 중 PostgreSQL 완료와 재개 후 Outbox catch-up 검증

## 한계와 운영 판단

- 실제 수분 이상 PostgreSQL 전면 장애, 연결 복구 직전 Worker 종료, DB commit
  응답 유실의 모든 조합을 인증한 것은 아니다. DB가 돌아오지 않으면 Worker가 최종
  실패 상태를 커밋할 수도 없으며 lease recovery가 소유권을 정리한다.
- 고아 결과 파일 자동 회수·삭제·재연결 기능은 이번 범위에 없다. fencing 경로 덕분에
  후속 Attempt의 결과로 잘못 채택되지는 않는다.
- Redis 장기 장애 실검증은 격리 환경에서 outage smoke를 수행해야 한다. 단위 테스트
  통과만으로 운영 Redis의 persistence·failover 설정을 인증하지 않는다.
- 파일시스템 고장, PV 분리, 노드 강제 종료 후 실제 durability는 스토리지 클래스와
  운영 인프라에도 의존한다. 배포 전 대상 PV에서 별도 장애·soak 검증이 필요하다.

## 2026-09-02 검증 결과

- Ruff lint/format 및 ty 통과.
- 기본 회귀 520개 통과, 선택형 live Jupyter 테스트 4개 제외.
- 실제 Redis 통합 10개 통과.
- 일회성 PostgreSQL DB의 migration, 다중 Worker, Outbox 통합 24개 통과.
- 실제 장기 DB/PV 장애와 Redis pause smoke는 이번 로컬 실행에 포함하지 않았다.
