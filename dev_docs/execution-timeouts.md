# Execution 시간 제한

아래 기본값보다 서비스에 설정된 환경변수가 우선한다.

## 요청 필드

| 필드 | 조건 | 현재 측정 구간 |
| --- | --- | --- |
| `operation.spec.steps[].step_timeout_seconds` | 선택, 정수 1초 이상, 생략/null이면 이 제한 없음 | Step 결과 저장 준비 → 런타임 실행·출력 수집 → 결과 파일 확정 |
| `operation.operation_timeout_seconds` | 선택, 정수 1초 이상, 생략/null이면 이 제한 없음 | Operation RUNNING 전환부터 경과한 시간을 각 Step 실행 시 예산에서 차감 |
| `lifecycle.operation_wait_timeout_seconds` | MULTI 필수, 정수 30초 이상, SINGLE 사용 불가 | WAITING_FOR_OPERATION 진입 후 다음 Operation/finalize 요청 대기 |

후속 Operation POST는 `operation` wrapper 없이 `operation_timeout_seconds`와
`spec`을 body 최상위에 받는다. 모든 값은 초 단위다. `300000`은 약 3.47일이며
밀리초가 아니다. 예시의 300/600은 자동 기본값이 아니다. 0은 무제한 표시가 아니다.
요청 모델에 이 필드들의 별도 최댓값은 없지만 서비스 전체 수명 제한은 적용된다.

## 실제 적용 범위

- Step마다 `min(Step 제한, Operation 잔여 예산)`을 적용한다. null인 제한은 제외한다.
- Operation의 시작점은 코드 전송 순간이 아니라 Worker의 RUNNING 전환이다.
  준비 및 Step 간 처리 시간이 잔여 예산을 소비한다.
- 현재 구현은 Operation 전체를 별도 타이머로 감싸지 않는다. 각 Step 실행에
  적용하므로 준비·최종 노트북 반영·정리까지 정확히 N초 내에 끝낸다는 보장은 없다.
- `Operation timeout expired after 105.82 seconds`의 숫자는 그 Step에 적용된
  잔여 예산일 수 있다. 원래 요청 제한이나 전체 경과시간으로 단정하지 않는다.
- Step이 다른 Executor로 인계되어도 제한은 원래 Step/Operation 시작 시각 기준이다.
  인계로 멈춘 시간도 예산을 소비한다.
- 타임아웃 후 interrupt·idle 확인·필요한 커널 삭제·결과 보존·DB 갱신이 이어져
  terminal 이벤트는 제한시간보다 늦게 도착할 수 있다.
- Step/Operation 초과는 `STEP_TIMEOUT`/`OPERATION_TIMEOUT`으로 분류한다.
  SINGLE은 실패 후 재시도 정책을 확인한다. MULTI는 중단 후 커널 재사용이 안전하면
  실패 Operation을 종료하고 다음 보정 Operation을 기다릴 수 있다. 커널 유지가
  불가능하면 Execution을 FAILED로 종료한다. 모든 timeout이 MULTI 대기를 허용하는
  것은 아니다. 이벤트 continuation과 상세 상태를 확인한다.

## MULTI 대기 및 Execution 전체 제한

```text
전체 만료 = 최초 Worker 실행 시작 + EXECUTION_MAX_RUNTIME_SECONDS
MULTI 대기 만료 = min(대기 진입 + 요청 대기시간, 전체 만료)
```

- 전체 제한 기본값은 604800초(7일)이며 요청으로 별도 받지 않는다.
- 최초 배정 전 QUEUED에는 별도 만료가 없다.
- 최초 실행 이후 MULTI 대기·재시도 간격도 전체 예산을 소비한다.
- 각 Operation 완료 후 새 대기 기한을 계산한다. 다음 Operation/finalize 접수 시
  기존 대기 기한을 해제한다. 조회만으로 연장되지 않는다.
- Agent 장애를 직접 감지하는 소유권 heartbeat는 없다. 실행 중에는 커널 감시와
  실행 제한을 따르고, 대기 중에는 다음 요청 대기 기한을 따른다.
- 대기 중 만료는 FAILED/OPERATION_WAIT_TIMEOUT이며 전체 수명도 만료됐다면
  FAILED/EXECUTION_TIMEOUT 경로를 사용한다.
- 전체 수명 점검에서 이미 만료 시각이 있는 QUEUED/RUNNING은 CANCEL_REQUESTED로
  바꾼 뒤 CANCELLED로 종료한다. 최대 시간 초과 사유는 cancellation_reason에 남는다.
  WAITING 경로와 최종 상태가 다르다는 것이 현재 동작이다.
- 전용 전체 수명 스캔의 대상에 FINALIZING은 직접 포함되지 않는다. 상태 전환과
  주기 점검 방식이므로 정확한 시각의 강제 종료 SLA로 해석하지 않는다.

## retry와 보존 커널

- SINGLE retry는 재실행 Step의 시작·종료 시각을 초기화하고 Operation 시작·예산을
  새로 설정한다. Execution 최초 시작·전체 만료 시각은 유지한다.
- 유지 가능한 실패 커널의 기본 보존시간은 3600초
  (`FAILED_SESSION_RETENTION_SECONDS`)다. 모든 실패 커널에 적용되는 것은 아니다.
- FROM_FAILED_STEP은 보존 기한과 커널 유효성이 필요하다. retry API로 timeout을
  변경하거나 전체 수명을 연장하는 옵션은 없다.
- Agent는 오류 메시지 숫자만으로 재시도를 결정하지 않고 retry/continuation을 본다.

## 실행시간과 다른 운영 타이머

| 설정 | 기본값 | 용도 |
| --- | --- | --- |
| `EXECUTION_LEASE_SECONDS` / `EXECUTION_HEARTBEAT_SECONDS` | 60초 / 15초 | Worker 실행 소유권. 코드 진척도나 Agent 생존 신호 아님 |
| `JUPYTER_STREAM_RECONNECT_SECONDS` | 30초 | 실행 저널 스트림이 끊긴 뒤 재연결을 시도하는 시간. 커널 소실 판정은 Jupyter 확장이 함 |
| `JUPYTER_REQUEST_TIMEOUT_SECONDS` | 30초 | 일반 HTTP 통신 제한. 장기 코드 실행 제한 아님 |
| `JUPYTER_STORAGE_TIMEOUT_SECONDS` | 300초 | 파일/노트북 HTTP 통신 제한. 전체 작업 수명 아님 |
| `RUNTIME_ABORT_TIMEOUT_SECONDS` | 15초 | interrupt 요청 이후 idle 확인 대기. interrupt HTTP 시간 자체는 별도 |
| `RUNTIME_CLEANUP_RETRY_INTERVAL_SECONDS` | 60초 | 실패한 커널 정리 재시도 간격 |

MULTI 만료·복구·보존 커널 정리는 기본적으로 heartbeat 간격의 점검 루프를 사용한다.
점검 작업 후 sleep하므로 정확히 매 15초 또는 만료 즉시 처리되는 것은 아니다.
출력이 없다는 이유만으로 장기 계산을 중단하지는 않는다.

## 시각과 결과 해석

- UTC 시각을 사용한다. `created_at`, `started_at`, `finished_at` 차이는 상태 경과시간이지
  순수 함수 계산시간이 아니다. 전용 코드 계산시간·대기시간 누적 필드는 제공하지 않는다.
- `updated_at`은 마지막 코드 출력 시각이 아니다. Attempt heartbeat는 Worker 갱신이다.
- 이벤트 `occurred_at`, Redis 발행 시각, Agent 수신 시각은 서로 다르다.
- CANCELLED에서도 커널 정리가 실패할 수 있다. `recovery`와 diagnostics를 확인한다.
- 노트북 Artifact가 AVAILABLE이어도 최신 출력 반영 여부는 별도다. 실패·취소 후에는
  `workspace.notebook_projection`과 봉인된 부분 결과 ref를 함께 확인한다.
