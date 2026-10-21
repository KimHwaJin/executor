# 실행 소실 감지

## 목적

커널 프로세스가 종료되거나 같은 Kernel ID로 재시작됐는데 Executor가 완료 메시지를
기다리며 RUNNING으로 남는 것을 방지한다. OOM 원인 조사, CPU/메모리 샘플링, 커널에
코드 삽입, 자동 코드 재실행은 하지 않는다. 무출력 시간/낮은 CPU/last_activity/
busy 또는 idle 값만으로 실패를 판정하지 않는다.

## 동작

Jupyter 확장이 Step마다 실행 저널을 기록하고, 소실 판정도 확장이 한다
([Step 인계](execution-recovery.md#step-handover-between-workers)).

1. Executor는 `POST /executor/kernels/{kernel_id}/executions`로 코드를 한 번만 보낸다.
   커널이 없거나 살아 있지 않으면 404이고 코드를 보내지 않는다.
2. 확장은 실행 시작 시 커널의 instance_id를 기억하고, 출력이 없는 동안 5초마다
   다시 조회한다. 프로세스 종료/종료 절차 진행/인스턴스 변경이면 저널에 `LOST`를 쓴다.
3. Jupyter 서버가 재시작되면 이전 서버가 시작한 미완료 저널은 끝날 수 없다.
   새 서버는 그 저널을 읽을 때 `LOST`("The Jupyter server restarted during execution.")를
   덧붙인다.
4. Executor는 저널 스트림에서 `LOST`를 받거나 저널이 없으면(404) 실행 소실로 처리한다.
   스트림 연결이 `JUPYTER_STREAM_RECONNECT_SECONDS`(기본 30초) 동안 되지 않으면
   `RuntimeDriverError`로 처리한다.
5. 기존 결과 저장, 실패 처리, lease fencing, DB/Outbox 경로로 종료한다. 기존 성공
   Step은 보존하고, 실행 중 저장한 부분 출력은 complete=false로 남긴다.

확인된 실행 소실은 기존 RUNTIME_SESSION_LOST로 분류한다. SINGLE은 기존 FROM_START
정책을 사용하고 MULTI는 기존 인프라 오류 정책에 따라 NOT_RETRYABLE이다. 자동 코드
재실행은 없고, 소실된 메모리를 전제로 FROM_FAILED_STEP으로 재개하지 않는다.
기존 execution.step_completed / execution.operation_completed / execution.completed
이벤트와 failure/diagnostics에 사유를 남긴다. 정리 실패는 기존 cleanup 재시도 체계로
관리한다. 완료/오류/취소/타임아웃 시 감시 태스크도 취소하고 회수한다.

## 커널 종료 사유

커널 프로세스가 끝나서 `LOST`를 쓸 때, 확장은 실행 시작 때 잡아 둔 그 프로세스의 종료
상태를 메시지에 덧붙인다. 새 폴링이나 메모리 샘플링은 없고, 실행 시작과 종료 감지
시점에 파일 하나씩만 읽는다.

- 종료 코드: `It exited with code N.`
- 시그널: `It was killed by SIGKILL.` 등
- SIGKILL이고 실행 중 컨테이너 메모리 cgroup이 한도에 닿았으면(cgroup v2
  `memory.events`의 `oom_kill`, 없으면 cgroup v1 `memory.failcnt`가 증가):
  `... most likely killed for running out of memory.`
- 같은 Kernel ID 수동 재시작처럼 종료 상태를 알 수 없으면 덧붙이지 않는다.

OOM killer는 항상 SIGKILL을 쓰므로 메모리 부족을 강하게 시사하지만, 프로세스별 OOM
기록이 없는 cgroup v1(노드 커널 3.10)에서는 추정이다. 같은 시점의 수동 `kill -9`와는
구분하지 못한다. Executor는 이 메시지를 그대로 Step/Execution 오류
(`RUNTIME_SESSION_LOST`)로 남긴다.

## 커널 식별 (확장 내부)

확장은 `KernelStateObserver`로 커널 프로세스의 생존과 instance_id를 확인한다. 별도 HTTP
API는 두지 않는다(이전의 `GET /executor/kernels/{kernel_id}/execution-state`는 Executor가
더 이상 호출하지 않아 제거했다).

- LocalProvisioner의 process 객체와 KernelManager.is_alive()/shutting_down을 사용한다.
  같은 Kernel ID 또는 PID라도 프로세스가 교체되면 다른 식별값이 된다.
- 관측 불가능한 remote/custom provisioner에서는 실행을 시작하지 않는다. 조용히 감시를
  생략하지 않는다.

## Executor 설정과 장애 구분

| 환경변수 | 기본값 | 의미 |
|---|---:|---|
| JUPYTER_STREAM_RECONNECT_SECONDS | 30 | 저널 스트림 재연결을 포기하기까지의 시간(초) |

이전의 Executor 쪽 커널 주기 조회(`JUPYTER_EXECUTION_POLL_SECONDS` 등)는 확장 내부
감시로 대체되어 제거됐다. 출력이 오는 동안에는 조회하지 않는다.

## 배포와 외부 계약

Jupyter 확장과 Executor 둘 다 변경된다. **확장을 먼저 설치/이미지 재빌드하고
Jupyter 서버를 재시작한 후 Executor를 배포한다.** 기존 실행이 있는 서버는 먼저
drain/종료 계획을 따른다. 구버전 확장에 새 Executor만 연결하면 실행 시작 요청이
404로 실패하고 코드를 보내지 않는다. 확장 없이 실행하는 fallback은 없다.

Execution REST/MCP 스키마는 변경하지 않는다. Step 인계 이벤트 두 종류와 인계용 테이블이
추가됐다.
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

- OOM 확정/개별 커널 OOM 귀속은 하지 않는다. 위의 종료 사유는 추정이다.
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
