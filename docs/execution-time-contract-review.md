# Execution 시간 정책 검토

- 기록일: 2026-09-21
- 상태: 논의 중. 아래 후보는 구현 승인 또는 확정 계약이 아니다.
- 현재 결정: 소요시간 측정·DB 확장은 보류하고, API 실행시간 제한 정책과
  함께 검토한 뒤 반영 범위를 결정한다.
- 이번 변경: 문서만 작성. API, DB, 실행 로직 및 환경변수는 변경하지 않는다.
- 후속 결정: Agent 측 주기적 heartbeat/소유권 갱신 추가안은 채택하지 않는다.
  Agent 장애를 추정해 실행 중 작업을 자동 취소하는 새 로직도 도입하지 않는다.
  실행 중은 기존 커널 감시·실행 제한, MULTI 대기는 후속 요청 대기 제한을 따른다.
- 구현된 시간 계약은 [개발자 가이드](../dev_docs/execution-timeouts.md)에 별도로 정리한다.

## 1. 소요시간의 의미와 저장 방식 — 보류

현재 `created_at`, `started_at`, `finished_at`은 객체 생성 및 Executor의
상태 전환 시각이다. 둘의 차이를 순수 코드 계산시간으로 표현하면 안 된다.

| 후보 구분 | 의미 | 구현 영향 |
| --- | --- | --- |
| 전체 경과시간 | 요청 접수부터 최종 종료까지 | 기존 시각으로 계산 가능 |
| 최초 실행 배정 대기시간 | 접수부터 최초 Worker 실행 시작까지 | 기존 시각으로 계산 가능 |
| 실행 처리시간 | 준비, 런타임 실행, 출력 저장, 노트북 반영 등 | 구간 정의 후 측정·저장 방식 결정 |
| Agent 대기시간 | MULTI의 다음 Operation/finalize 요청 대기 누적 | 전환 시 측정 및 DB 저장 검토 |
| 재시도 대기시간 | 실패 후 retry 요청까지의 대기 누적 | 이력 활용 또는 전용 시각·누적값 검토 |
| 런타임 실행 경과시간 | 실행 요청 전송부터 완료 응답·출력 수신 완료까지 | 측정 로직 및 조회용 저장 검토 |

- 런타임 실행 경과시간도 통신·출력 전달을 포함하므로 순수 계산시간이 아니다.
- 런타임 실행시간은 실행 처리시간의 부분집합이다. 중복 합산하지 않는다.
- 전체 시간을 위 구간의 합으로 설명하려면 후속 Operation 배정 대기,
  실패 복구, 취소·정리 구간까지 중복·누락 없이 정의해야 한다.
- `updated_at`은 마지막 출력 시각이 아니고, Worker heartbeat는 코드 진척도가 아니다.
- 재시도 전후 시간은 현재 객체의 시각뿐 아니라 Attempt/StepAttempt 이력을
  구분해야 한다. MULTI의 하나의 Attempt에는 여러 Operation과 대기가 포함될 수 있다.
- 이벤트 보관 기간에 의존하는 역산을 공식 소요시간 원본으로 삼지 않는 방향을
  제안했지만, 최종 저장 스키마·응답 필드·마이그레이션은 아직 결정하지 않았다.

## 2. 현재 API 시간 제한 — 코드 확인 사실

| 필드 | 현재 규칙 |
| --- | --- |
| `operation.spec.steps[].step_timeout_seconds` | 선택, null이면 해당 제한 없음, 정수 1초 이상 |
| `operation.operation_timeout_seconds` | 선택, null이면 해당 제한 없음, 정수 1초 이상 |
| `lifecycle.operation_wait_timeout_seconds` | MULTI 필수, 정수 30초 이상, SINGLE에서는 지정 불가 |

- 모든 요청 값은 초 단위다. Step/Operation에 300초 자동 기본값은 없다.
- 후속 Operation도 동일한 Operation/Step 제한 입력을 사용한다.
- Step 제한은 결과 저장 준비부터 런타임 실행·출력 저장·결과 확정까지를 감싼다.
- Operation은 Worker가 RUNNING으로 전환한 시각부터 예산을 계산한다.
  각 Step 실행에서 Step 제한과 Operation 잔여 예산 중 작은 값을 적용한다.
  준비·마지막 후처리 전체에 독립적인 Operation 타이머가 적용된 것은 아니다.
- MULTI 대기는 WAITING_FOR_OPERATION 전환 시 시작하고 각 Operation 완료 후
  새로 계산한다. 전체 Execution 만료를 넘지 않으며 조회만으로 연장되지 않는다.
- `EXECUTION_MAX_RUNTIME_SECONDS` 기본값은 432000초(5일)이다.
  최초 Worker 실행 시작부터 계산하고 retry/후속 Operation으로 연장하지 않는다.
  최초 큐 대기에는 별도 만료가 없다. 요청의 Execution 전체 제한 필드는 없다.
- retry 시 Operation 시작 시각은 재설정되지만 Execution 시작·만료는 유지된다.
- 전체 수명 만료는 대기 중 FAILED/EXECUTION_TIMEOUT, QUEUED/RUNNING에서는
  취소 요청 후 CANCELLED 경로로 처리되는 차이가 있다.

## 3. 다음 논의에서 결정할 사항 — 미확정

1. 각 제한의 적용 범위: 준비·결과 저장·노트북 반영·정리까지 포함할지.
2. 시작·종료 기준: Worker 배정, 커널 요청, 결과 확정 중 어떤 시각인지.
3. 생략/null/최솟값/최댓값과 서비스 전체 안전 한도의 관계.
4. retry 시 예산 재시작 여부 및 MULTI 대기시간 갱신·연장 정책.
5. Execution 전체 시간 제한을 서비스 설정으로만 둘지 요청에서도 받을지.
6. 만료 시 SINGLE/MULTI 상태, 오류 유형, 커널 유지·중단·삭제 처리.
7. 제한시간 초과와 이후 안전 정리시간을 구분할지.
8. 오류 정보에 설정값·실제 경과시간·잔여 예산·단계를 어떻게 제공할지.
9. 확정된 제한 기준과 1번 소요시간 측정 기준을 맞춘 뒤 필요한 DB/API 변경 결정.

## 코드 참조

- `src/executor_service/interfaces/_contracts/execution_inputs.py`
- `src/executor_service/execution_specs.py`
- `src/executor_service/settings.py`
- `src/executor_service/infrastructure/execution_worker/step_executor.py`
- `src/executor_service/infrastructure/execution_worker/claiming.py`
- `src/executor_service/infrastructure/execution_worker/multi_operation_state.py`
- `src/executor_service/infrastructure/execution_worker/multi_lifecycle.py`
- `src/executor_service/domain/models.py`
