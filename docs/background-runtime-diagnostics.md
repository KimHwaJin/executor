# 백그라운드 Runtime 진단 — Phase 5

## 변경 목적

실행 중인 Worker 외에, 재시작 후 남은 커널 정리·보존기간 만료 정리·MULTI 대기
상태 점검에서도 실제 오류 원인을 남긴다. 예전의 일반 warning이나 조용히 넘어가던
연결 오류를 기존 `execution_diagnostics` 이력과 비밀값을 제거한 로그로 확인한다.

새 API·MCP Tool·이벤트·DB 컬럼·마이그레이션은 없다. 현재 DB head는 `0003`이다.
조회는 기존 `GET /api/v1/executions/{id}/diagnostics`를 사용한다.

## 적용 경로

| phase | 의미 |
| --- | --- |
| `RECOVERY_VALIDATE` | 정리 대상의 현재 Execution/Runtime 식별 정보 확인 실패 |
| `RECOVERY_TARGET` | 정리해야 할 Runtime Target이 없거나 참조가 누락됨 |
| `RECOVERY_DRIVER_CREATE` | Runtime 클라이언트 생성/자격증명 로딩 실패 |
| `RECOVERY_SESSION_DELETE` | 남아 있던 커널/세션 삭제 실패 |
| `RECOVERY_DRIVER_CLOSE` | 삭제 요청 이후 클라이언트 연결 종료 실패 |
| `RECOVERY_RESULT_PERSIST` | 실제 삭제 결과를 DB 상태에 반영하지 못함 |
| `MULTI_DRIVER_CREATE` | 대기 세션 점검용 Runtime 클라이언트 생성 실패 |
| `MULTI_SESSION_PROBE` | 대기 세션 존재 여부를 확인하는 API 호출 실패 |
| `MULTI_DRIVER_CLOSE` | 대기 세션 점검 후 클라이언트 연결 종료 실패 |
| `MULTI_AUDIT` | 개별 대기 Execution 처리의 기타 오류 |

대상 목록 DB 조회부터 실패하여 Execution을 특정할 수 없으면 잘못된 실행에
진단을 붙이지 않는다. `LEASE_RECOVERY_SCAN`, `RETAINED_CLEANUP_SCAN`,
`MULTI_LIFECYCLE_SCAN` 단계의 구조화 로그만 남긴다.

## 상태를 해석하는 방법

- 일시적인 MULTI 연결 장애는 `WAITING_FOR_OPERATION`을 유지하고 진단만 남긴다.
  기존 대기 만료시각과 Execution version은 바꾸지 않는다.
- 세션이 실제로 없다는 응답을 받으면 기존 `RUNTIME_SESSION_LOST` 실패 규칙을 따른다.
  통신 실패와 존재하지 않는다는 확정 응답을 구분한다.
- 원래 `TOOL_ERROR`와 정리 중의 `PERMISSION_DENIED` 등은 별개다. 정리 오류로
  기존 코드 오류를 덮어쓰지 않는다.
- 커널 삭제 성공 후 클라이언트 `close()`만 실패하면 cleanup 상태는 SUCCEEDED일 수
  있다. 연결 해제 오류가 커널 삭제 성공 자체를 취소하지는 않는다.
- DB에 삭제 결과를 반영하지 못하면 PENDING 예약을 남겨 후속 정리 루프가 재확인한다.
  DB까지 계속 장애라면 원인과 `DIAGNOSTIC_PERSIST` 로그만 남을 수 있다.
- `severity=ERROR`는 관측된 오류의 수준이지 Execution의 최종 상태가 아니다.
  diagnostics 건수만으로 실행을 실패 처리하거나 코드를 재실행하면 안 된다.

## 오래된 관측과 경쟁 상태 보호

내부 `RuntimeObservation`은 실행 상태, version, fencing_token, Operation, Target,
session ID를 캡처한다. 사용자/Agent가 요청에 이 값을 넣을 필요는 없다.

진단 저장·정리 결과 반영·대기 만료 실패 전이 직전에 현재 DB 값과 다시 비교한다.
값이 달라졌다면 새 Operation/Attempt/커널에 과거 관측을 적용하지 않는다.
진단은 version을 올리지 않으며, 기존 active lease용 `DiagnosticRecorder`의
검증을 느슨하게 바꾸지 않는다. 백그라운드 경로만 별도 optimistic 검증을 사용한다.

보존기간 만료 정리는 DB row lock 아래에서 먼저 NOT_RETRYABLE/PENDING으로
예약하고 보존기간을 해제한 뒤 커널을 삭제한다. 삭제 전에 같은 커널로 재시도가
들어가는 경쟁을 차단한다. 아직 큐에 있던 만료 retry는 FAILED로 닫고 기존 완료
이벤트를 1회 발행한다. 배치당 최대 20개이며 원격 I/O 중 DB lock을 잡지 않는다.

원격 Jupyter 삭제와 DB 갱신을 하나의 분산 트랜잭션으로 만드는 것은 아니다.
프로세스 사망이나 재시도 간격보다 오래 걸리는 삭제 때문에 원격 삭제가 재요청될 수
있다. 대상 예약과 version 비교는 DB 오염을 막으며, 원격 삭제는 기존 멱등적 처리를
전제로 한다. 임의 수동 DB 변경까지 원격 동작을 원자적으로 막는 보장은 없다.

## 중복·부하 제한

- 동일 Execution 세대/Operation/phase와 동일한 정제된 원인 정보는 5분 내 중복
  저장을 제한한다. 원인이 바뀌면 새 관측을 허용한다.
- 프로세스별 TTL 캐시는 최대 1,024개다. 반복 오류는 DB와 로그 호출도 줄인다.
- DB row lock 안에서 최근 5분의 같은 phase 이력 최대 128건을 비교하므로,
  프로세스가 여러 개여도 중복을 억제한다. 최근 이력 비교는 기존 Execution 시간
  인덱스를 사용하며, 무제한 이력을 읽지 않는다.
- DB 진단 작업은 2초 deadline을 유지한다. DB 장애 시 원인과 저장 실패를
  안전한 로그로 남기고 동일 오류는 로컬에서 5분 동안 backoff한다. 이 기간의
  실패가 DB 복구 뒤 자동 재전송되는 구조는 아니다.
- 정상 세션 점검에 진단 insert나 추가 결과 파일 생성은 없다. 점검 주기·cleanup
  retry 간격은 기존 설정을 유지한다.
- 같은 장애를 매번 세는 메트릭/감사 이벤트가 아니며 suppressed_count, last_seen,
  자동 TTL 삭제는 추가하지 않는다. 유일한 incident ID로 사용하면 안 된다.

## 검증과 한계

- 일반 회귀와 실패 주입: 원인 보존, 지연된 관측 차단, 만료 예약 선행, 장애 대상
  이후 배치 계속 처리, DB 장애 deadline/로그 보호, 중복 제한/캐시 크기 검증.
- PostgreSQL: 독립 Recorder 8개의 동시 기록은 1건, Worker 2개의 동시 만료 정리는
  삭제 1회. 실제 row lock과 트랜잭션으로 검증한다.
- 실제 Jupyter: basic/ML의 SINGLE/MULTI 텍스트·PNG, 추가 Operation/finalize,
  원래 출력 제한, MULTI 연결 장애·중복 진단·대기 만료 삭제를 검증한다.

실제 검증 스크립트는 `scripts/jupyter_output_completeness_smoke.py`다.
`JUPYTER_GATEWAY_ENDPOINT`, `JUPYTER_GATEWAY_TOKEN`, `JUPYTER_GATEWAY_PROFILE`을
설정해 실행한다. 추가된 background 시나리오는 오직 임시 SQLite DB의 Target을
loopback 폐쇄 포트로 변경했다가 복원하며, 가동 중인 Executor DB나 실제 Jupyter
설정은 바꾸지 않는다. 자기 Execution의 커널만 정리한다.

기존 서비스 DB/Redis/컨테이너는 변경하지 않았다. 장애 주입은 제한된 시나리오이며
며칠 단위 soak, 프로세스 kill 모든 시점, 전체 DB 장애 복구, 알 수 없는 Runtime
동작까지 완전하게 검증했다는 의미는 아니다.

### 2026-08-31 실행 결과

- Ruff/lint/format, ty 통과.
- 일반 회귀 382개 통과(신규 백그라운드 오류/보호 테스트 21개 포함).
- PostgreSQL/Redis 통합 32개 통과.
- 실제 Jupyter basic 7개, ml 7개로 총 14개 통과.
- background 시나리오: 연결 실패 2회에도 진단은 1건, WAITING/version 유지,
  연결 복원 후 의도적으로 대기시간 만료 → OPERATION_WAIT_TIMEOUT → 실제 커널
  정리 SUCCEEDED. 이 케이스의 Execution FAILED는 의도한 테스트 결과다.

실제 background 검증 ID:

- basic: `7e406080-8201-46c5-ab51-74ae7c66dd60`
- ml: `8cad62db-c5c0-49ec-a430-199d9b16d034`

노트북은 Jupyter PV의
`users/diagnostics-smoke/projects/output-completeness/sessions/.../executions/{id}/notebooks/execution.ipynb`
에 남는다. 임시 테스트 DB/공유 결과는 종료 시 제거되므로 이 ID를 기존에 가동 중인
Executor API로 조회할 수 있는 테스트는 아니다.
