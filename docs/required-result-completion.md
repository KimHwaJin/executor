# 필수 결과 완료 정책

## 목적과 범위

코드가 정상 종료했더라도 필수 결과 전달이 실패하면 전체 성공으로 보고하지 않는다.
노트북 작성 실패를 무시하던 경로와 노트북 Artifact 등록 예외를 삼키던 경로를
차단한다. 성공한 코드를 다시 실행해 저장/정리 장애를 복구하지 않는다.

| 단계 | 성공 확정 기준 |
| --- | --- |
| Step | 코드 성공 및 공유 PV 출력/manifest 저장 완료. 기존 `result_ref` 유지 |
| Operation | 소속 Step 성공/완전한 참조, Jupyter PV 노트북 저장, 실제 발견된 Artifact 등록 |
| Execution | 위 기준과 최종 노트북 Artifact 등록, 런타임 세션 해제 완료 |

SINGLE은 Operation/Execution 종료가 연속으로 처리된다. MULTI는 성공 Operation마다
WAITING_FOR_OPERATION으로 돌아가며, 노트북 파일은 계속 갱신한다. 변경 중인 노트북을
매 Operation마다 불변 Artifact로 등록하지 않고, 최종 finalize 때 등록한다.

최종 성공은 검사 시점의 보장이다. 이후 사용자가 파일을 삭제하거나 스토리지 자체가
손상되는 경우까지 막지는 않는다. 파일 저장과 PostgreSQL 사이의 분산 트랜잭션은 없다.

## 실패 처리

- 필수 노트북 작성은 기존 3회 저장 재시도를 유지한다. 이는 코드 재실행이 아니다.
- 노트북 조립/작성, 실제 발견된 Artifact 등록, 최종 노트북 등록/런타임 해제 실패는
  `failure_type=COMPLETION_FAILED`, `retry_strategy=NOT_RETRYABLE`로 종료한다.
- 이미 성공한 Step은 SUCCEEDED와 완전한 result_ref를 유지한다. 아직 실행하지 않은
  후속 Step은 SKIPPED다. 예: Step은 SUCCEEDED지만 Operation/Execution은 FAILED일 수 있다.
- 코드 자체의 오류/timeout/output-limit 뒤 best-effort 노트북 작성도 실패했다면
  원래 실행 실패 유형을 유지하고, 후속 실패는 별도 진단 이력에 남긴다.
- Lease 소유권 상실/취소는 기존 fencing/cancellation 경로를 유지한다.
- 협조적 취소/Worker 종료 시 노트북은 마지막 반영본에 머물 수 있다. 최신 Step을
  미반영한 상태로 이전 projection 성공 표시를 유지하지 않고 `FAILED`와 사유를
  남긴다(`NOTEBOOK_NOT_REFRESHED / NOTEBOOK_INTERRUPTED`). 부분 출력 원본은
  공유 PV result_ref에서 읽으며, 취소·종료 중 자동 노트북 재생성은 하지 않는다.
- 최종 정리가 처음 실패했지만 best-effort 정리가 성공해도 해당 Execution은 보수적으로
  FAILED다. cleanup_status는 실제 정리 결과를 표시하고 원래 실패 진단은 보존한다.

`GET /api/v1/executions/{id}/diagnostics`에서 phase와 원인 진단을 확인한다.
주요 phase: NOTEBOOK_BUILD, NOTEBOOK_WRITE, ARTIFACT_REGISTER,
NOTEBOOK_ARTIFACT_REGISTER, RUNTIME_RELEASE. 성공 전이 보호에 걸린 경우
RESULT_COMPLETION_CHECK, OPERATION_COMPLETION_CHECK, NOTEBOOK_COMPLETION_CHECK,
NOTEBOOK_ARTIFACT_CHECK로 구분한다. 결과 파일 봉인 후 Step 결과 참조와 성공
이벤트의 DB 트랜잭션이 실패하면 RESULT_REFERENCE_PERSIST로 구분한다.

## MULTI finalize

마지막 Operation의 Step 중 실패/취소/미완료가 있으면 finalize API는 409다.
보정 Operation을 성공시킨 뒤 finalize하거나 cancel할 수 있다. 이전 실패 이력은
삭제하지 않으며, 모든 과거 Step 성공을 강제하지 않는다.

최종화 시 공유 결과를 다시 읽어 노트북을 작성하고 최종 Artifact를 등록한다.
manifest/출력 파일이 누락되거나 손상되어 읽을 수 없으면 성공 처리하지 않는다.
이미 성공 통지된 Operation은 나중의 finalize 실패로 소급 변경하지 않는다.
Execution terminal 이벤트를 전체 종료 결과로 사용한다.

## 구현과 부하

- `notebook_projector.project_required`는 정상 경로에서 필수 저장을 강제하고,
  `project_after_failure`는 원래 오류를 보존하는 best-effort 경로로 유지한다.
- `completion_policy.require_completed_results`는 fenced 성공 전이 직전에 현재
  Operation의 Step/ref와 notebook 상태/최종 Artifact를 검사한다.
- DB 잠금 안에서 원격 파일 I/O를 하지 않는다. 성공 경계의 조회만 추가하며, 각 출력
  청크나 주기적 폴링에 추가 DB 쓰기를 넣지 않는다. 최종 MULTI에는 전체 노트북
  재투영 1회가 추가된다. 큰 결과의 재투영 비용은 별도 성능 검증 대상이다.
- Request/이벤트/manifest 스키마 버전은 `1.0` 유지. 신규 오류 분류와 stricter 성공
  조건만 반영하며, 새 API·설정·모든 예상 Artifact 선언을 추가하지 않는다.

## 배포와 보류

`uv run alembic upgrade head` 후 새 애플리케이션을 배포한다. 현재 head `0003`은
Execution/Attempt의 failure-type CHECK만 확장하며 DB/Redis 초기화는 하지 않는다.
COMPLETION_FAILED 데이터가 있으면 0002로 downgrade는 실패/롤백되어 이력을 보호한다.

보류 사항: 코드 재실행 없는 노트북 재생성/후처리 복구 API, 예상 Artifact 선언,
필수 리포트 생성 정책, 전처리 데이터 저장 정책. 현재 실제 발견 파일이 없으면
추가 Artifact가 없다는 이유만으로 실패시키지 않는다.

## 검증

`tests/test_required_result_completion.py`는 SINGLE/MULTI 저장·등록·정리 장애,
일시 저장 장애 복구, 성공 코드 재시도 차단, MULTI 보정 후 finalize, 누락/불완전한
결과 참조와 누락된 최종 Artifact의 성공 전이 차단을 검증한다.
기존 failure evidence/output safety/lease/cancel 회귀도 함께 실행한다.

PostgreSQL 테스트는 격리 DB에서 0002→0003 데이터 보존, Execution/Attempt 새 실패값
저장, 증거 손실 downgrade 차단과 `alembic check`를 검증한다.
`scripts/jupyter_output_completeness_smoke.py`는 실제 basic/ML 커널의 텍스트·PNG,
MULTI 추가 Operation→finalize, 원래 출력 제한 정책을 검증한다. Worker를 직접
구동하며 실제 서비스 DB/Redis는 사용하지 않는다. 이벤트 저장/Outbox와 실제 Redis
발행은 별도의 통합 테스트로 검증한다.

2026-08-31 검증 결과:

- Ruff/lint/format 및 ty 통과.
- 일반 회귀 361개, PostgreSQL/Redis 통합 30개 통과.
- 실제 Jupyter 12개(basic 6, ml 6) 통과. 정상 PNG 출력 약 10.4 KiB는 노트북과
  공유 결과에서 일치했다. MULTI 정상 케이스는 2개 Operation과 finalize까지 확인했다.
- 5 MiB stdout 제한 케이스는 실패/불완전으로 처리하고 후속 Step을 건너뛰었다.
  Jupyter 출력 제한은 해제하거나 높이지 않았다.
- basic MULTI 확인 ID: `a6832641-0d30-487b-91f2-f192a6f09bbe`.
- ml MULTI 확인 ID: `c5f4b5c7-eda3-4849-bcfd-cf06d9bd005c`.

실제 테스트 노트북은 Jupyter PV의
`users/diagnostics-smoke/projects/output-completeness/sessions/.../executions/{id}/notebooks/execution.ipynb`
에 남아 있다. 테스트 DB와 공유 출력은 임시 경로라 종료 후 제거되며, 해당 ID를
현재 가동 중인 Executor API에서 조회하는 테스트는 아니다. 서비스 컨테이너/기존 DB/
Redis는 그대로이며, 장시간 soak·프로세스 강제 종료·DB 전면 장애의 보장은 별도다.
