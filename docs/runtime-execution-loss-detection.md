# 실행 소실 감지

## 목적

커널 프로세스가 종료되거나 같은 Kernel ID로 재시작됐는데 Executor가 완료 메시지를
기다리며 RUNNING으로 남는 것을 방지한다. OOM 원인 조사, CPU/메모리 샘플링, 커널에
코드 삽입, 자동 코드 재실행은 하지 않는다. 무출력 시간/낮은 CPU/last_activity/
busy 또는 idle 값만으로 실패를 판정하지 않는다.

## 동작

1. Step 실행 직전 해당 커널의 프로세스 생존 여부와 instance_id를 조회한다.
2. WebSocket 결과 수신과 병렬로 실행 중인 커널만 주기적으로 확인한다.
3. 프로세스 종료/종료 절차 진행/인스턴스 변경을 확인하면 실행 소실로 처리한다.
4. Jupyter 기본 WebSocket restarting/dead 알림은 parent request ID 필터보다 먼저
   처리한다. 서버 생성 알림에는 요청 parent ID가 없기 때문이다.
5. 실행 결과/코드 오류를 반환하기 직전에도 연속성을 확인한다. 수동 재시작이 먼저
   KeyboardInterrupt를 반환하는 경우를 코드 오류로 보고 커널을 재사용하지 않는다.
6. 기존 결과 저장, 실패 처리, lease fencing, DB/Outbox 경로로 종료한다. 기존 성공
   Step은 보존하고, 실행 중 저장한 부분 출력은 complete=false로 남긴다.

확인된 실행 소실은 기존 RUNTIME_SESSION_LOST로 분류한다. SINGLE은 기존 FROM_START
정책을 사용하고 MULTI는 기존 인프라 오류 정책에 따라 NOT_RETRYABLE이다. 자동 코드
재실행은 없고, 소실된 메모리를 전제로 FROM_FAILED_STEP으로 재개하지 않는다.
기존 execution.step_completed / execution.operation_completed / execution.completed
이벤트와 failure/diagnostics에 사유를 남긴다. 정리 실패는 기존 cleanup 재시도 체계로
관리한다. 완료/오류/취소/타임아웃 시 감시 태스크도 취소하고 회수한다.

## 내부 Jupyter API (Agent 공개 API 아님)

`GET /executor/kernels/{kernel_id}/execution-state`

Jupyter 기존 토큰 인증과 base_url을 사용한다. Cache-Control: no-store.

```json
{"kernel_id": "<Kernel ID>", "alive": true, "instance_id": "<opaque identity>"}
```

- 등록된 커널이 없거나 프로세스가 종료/교체/종료 절차 중이면 alive=false,
  instance_id=null이다. 이 API의 alive는 실행을 유지할 수 있는 상태를 뜻한다.
- LocalProvisioner의 process 객체와 KernelManager.is_alive()/shutting_down을 사용한다.
  같은 Kernel ID 또는 PID라도 프로세스가 교체되면 다른 식별값을 반환한다.
- 서버 재시작도 식별값이 달라진다. 프로세스 객체는 약한 참조로 관리한다.
- 관측 불가능한 remote/custom provisioner는 503이다. 조용히 감시를 생략하지 않는다.
- PID/명령행/인증정보/경로/OOM 정보는 노출하지 않는다. PV 파일도 생성하지 않는다.

## Executor 설정과 장애 구분

| 환경변수 | 기본값 | 의미 |
|---|---:|---|
| JUPYTER_EXECUTION_POLL_SECONDS | 10 | 실행 중 커널 조회 간격(초) |
| JUPYTER_EXECUTION_PROBE_TIMEOUT_SECONDS | 5 | 조회 한 번의 최대 대기(초) |
| JUPYTER_EXECUTION_PROBE_FAILURE_THRESHOLD | 3 | 실행 중 연속 조회 실패 한계 |

일시적 실패 뒤 성공하면 실패 횟수를 초기화한다. 연속 실패 한계를 넘으면
RUNTIME_UNAVAILABLE로 종료/커널 정리 절차에 들어간다. 이 경우 커널 종료가 아니라
관측 불가로 기록하며, OOM이나 프로세스 사망으로 단정하지 않는다.
실행 직전 관측이 불가능하면 코드를 보내지 않는다. 완료 직전 일시적 관측 실패도
같은 한계까지 재확인한다.

DB는 매번 갱신하지 않고 실패 시에만 기존 진단과 상태를 기록한다. 전체 서버/커널
스캔은 하지 않는다. 동시 활성 실행 30개면 기본 주기로 평균 약 3회/초의 상태 조회가
추가되며, Step 시작/완료 조회는 별도다. 감지 후 후처리와 통신 시간이 추가되므로
최종 DB 전이까지 정확히 10초를 보장하는 것은 아니다.

## 배포와 외부 계약

Jupyter 확장과 Executor 둘 다 변경된다. **확장을 먼저 설치/이미지 재빌드하고
Jupyter 서버를 재시작한 후 Executor를 배포한다.** 기존 실행이 있는 서버는 먼저
drain/종료 계획을 따른다. 구버전 확장에 새 Executor만 연결하면 초기 상태 조회에
실패하고 코드를 보내지 않는다. 감시 비활성화 fallback은 없다.

Execution REST/MCP 스키마, Redis 이벤트 종류/스키마, DB 테이블은 변경하지 않는다.
Agent의 새 API 호출/추가 파라미터는 필요 없다.

## 검증

단위 테스트는 동일 ID 재시작/종료/서버 재시작 식별, parent 없는 서버 알림,
정상 무출력 작업, 일시적/지속적 관측 장애, 구버전 확장, 취소 시 태스크 회수를 검증한다.

`scripts/runtime_execution_loss_e2e.py`는 격리 DB/Redis harness에서 새로 만든 커널만
자기 종료(SIGKILL) 또는 수동 재시작한다. SINGLE/MULTI의 실제 DB/Redis 실패 전이,
성공 결과와 불완전 출력 보존, 후속 Step 미실행, 타임아웃 없는 요청의 종료를 검증한다.
메모리를 고갈시키는 테스트는 하지 않는다.

```bash
# 전용 로컬 DATABASE_URL, EXECUTOR_REDIS_TEST_URL,
# REDIS_COMPAT_JUPYTER_ENDPOINT, REDIS_COMPAT_JUPYTER_TOKEN 설정 후
PYTHONPATH=scripts uv run python -c 'import asyncio; from redis_compatibility_lifecycle_smoke import main; asyncio.run(main(smoke_scripts=("runtime_execution_loss_e2e.py", "single_failure_retry_cancel_e2e.py", "multi_execution_lifecycle_e2e.py")))'
```

## 남는 한계

- OOM 확정/개별 커널 OOM 귀속은 이번 범위에 없다.
- 살아 있는 커널 내부의 교착/무한루프/자식 작업만의 소실은 기존 제한시간으로 보호한다.
- 활성 Step 실행 중 감시가 범위다. MULTI 대기 중 재시작된 프로세스의 메모리 연속성을
  다음 Operation까지 영속 추적하는 것은 별도 보완 사항이다. 기존 대기 중 세션 존재,
  서버 상태, 대기 제한시간 검사는 유지된다.

## 2026-09-10 검증 기록

- 작업 기준점: main `f443065`; 작업 브랜치 `fix/runtime-execution-loss-detection`.
- 전체 pytest 774 passed / 63 skipped(환경 의존), Ruff/ty 통과.
- 격리 PostgreSQL 스키마의 Alembic check, Compose 설정 검사 통과. DB 구조 변경 없음.
- 실제 Jupyter 2.21.0, Redis 6.0.8, PostgreSQL에서 다음을 확인했다.
  - 12초 무출력 Step 포함 SINGLE 정상 완료.
  - SINGLE/MULTI 커널 자기 종료(SIGKILL) → RUNTIME_SESSION_LOST/FAILED.
  - SINGLE/MULTI 동일 Kernel ID 수동 재시작 → RUNTIME_SESSION_LOST/FAILED.
  - 실패 Step의 부분 출력/manifest 보존, 이전 성공 Step 유지, 후속 Step 미실행.
  - DB 상태와 Redis terminal 이벤트 일치, 지연 cleanup 재시도의 커널 해제 완료.
  - 기존 SINGLE 실패 후 재시도/취소, MULTI 후속 Operation/오류 수정/finalize/취소 회귀.
  - 내부 관측 API 미인증 403, 없는 커널 200/alive=false/no-store.
- 실제 테스트에서는 감시 간격 2초, cleanup 재시도 1초를 사용했다. 운영 기본값은
  각각 10초/60초로 유지했으며, 제출부터 terminal까지의 시간에는 큐 대기도 포함된다.
- 수동 재시작에서 KeyboardInterrupt가 먼저 도착하는 경합과 Jupyter Manager의
  커널 부재 HTTPError(404)를 발견하여 최종 보완 및 재검증했다.
- 사용자 DB/Redis/기존 Compose 서비스는 초기화·교체하지 않았다. 새 UUID DB,
  전용 Redis 키, 전용 Jupyter 컨테이너를 이용했다.
