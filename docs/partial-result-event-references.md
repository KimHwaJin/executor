# 부분 결과 이벤트 참조 — Phase 6

## 변경 목적

출력이 불완전하다는 이유로 이미 저장된 증거의 위치까지 숨기지 않는다.
SINGLE/MULTI에서 Agent 등 외부 소비자는 Step/Operation 완료 이벤트의 참조를 사용해
코드와 보존된 텍스트·이미지를 직접 읽는다. Result API를 매번 추가 호출할 필요는 없다.

## 계약

- `execution.step_completed.payload.result_ref`
- `execution.operation_completed.payload.step_results[].result_ref`

두 참조에 필수 boolean `complete`를 추가한다. 이벤트 종류, 최상위 7개 필드,
`event_sequence`, 이벤트/manifest 버전 `1.0`과 Step/Operation 완료 시점은 유지한다.

```json
{
  "storage": "SHARED_PV",
  "relative_path": "executions/.../manifest.json",
  "media_type": "application/json",
  "size_bytes": 1842,
  "checksum_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "complete": false
}
```

`size_bytes`·checksum은 manifest 파일 기준이다. 실제 파일 크기를 예시 값으로
검증하면 안 된다. 출력 파일의 경로·크기·checksum은 manifest 내부를 사용한다.

| 조건 | 참조/상태 |
|---|---|
| 출력 정상 수집과 실행 성공 | `SUCCEEDED`, ref의 `complete=true` |
| 코드 오류까지 정상 수신 | `FAILED`, ref의 `complete=true` |
| timeout/취소/전송 제한 등으로 중단 | `FAILED` 또는 `CANCELLED`, ref의 `complete=false` |
| 봉인·DB 연결 실패 등으로 확정 참조 없음 | `result_ref=null` |

`complete`는 실행 성공 여부나 파일 쓰기 진행률이 아니다. 참조가 있는 manifest는
이미 봉인됐다. `complete=false`도 부분 증거로 읽을 수 있고, 반대로 `true`만으로
성공을 선언하면 안 된다. 성공 Step 이벤트에 불완전한 참조를 넣으면 계약 검증이 실패한다.

Step 이벤트의 `output_summary`는 참조와 함께 있거나 함께 null이다. count와 MIME
목록은 **실제로 보존된 출력**만 요약한다. 출력 0건인 manifest도 유효하며 이 경우
count=0/content_types=[]다. 텍스트·이미지 Base64·코드를 이벤트 본문에 복제하지 않는다.

## 취소 시 발견해 함께 수정한 누락

기존 취소 경로는 `abort_step_result()`로 파일을 봉인해도 반환된 descriptor를
DB에 연결하지 않고 버렸다. 따라서 파일이 있어도 Step/StepAttempt에서 찾지 못했다.

이제 deadline 소유자인 외부 `execute()`가 timeout과 취소를 구분해 한 번 봉인한다.
협조적 취소/Worker 중단이면 다음 순서로 처리한다.

1. 이미 받은 출력으로 terminal manifest를 봉인한다.
2. 실행 lease의 owner·fence·Attempt·유효기간을 검증하고 Execution을 잠근다.
   허용 상태는 RUNNING 또는 CANCEL_REQUESTED이며 양쪽 Step 행은 RUNNING이어야 한다.
3. Step/StepAttempt에 같은 참조와 보존 출력 요약을 저장한다. 이 단계는 실행 상태나
   이벤트 순번을 바꾸지 않는다.
4. 기존 취소/Worker 종료 처리가 terminal 상태와 완료 이벤트를 기록한다.

DB 참조 저장은 최대 2초이며 원격 Runtime 호출이나 파일 쓰기 중 DB lock을 잡지 않는다.
새 취소 Worker는 기존 execution lease가 해제되거나 만료된 뒤에만 소유권을 얻는 기존
규칙을 유지한다. lease를 잃은 Worker는 파일을 새 세대의 결과로 연결하지 못한다.
DB 연결 실패는 `RESULT_REFERENCE_PERSIST` 진단/안전 로그로 남기며 취소를 다른
실행 오류로 바꾸지 않는다. DB까지 장애라면 진단은 로그에만 남을 수 있다.

취소된 StepAttempt REST 상세/목록에도 저장된 참조를 노출한다. 신규 REST/MCP API나
DB 컬럼/마이그레이션은 없고 DB head는 `0003`이다.

## 소비자 규칙

1. 기존 event_id/Execution sequence 처리와 SINGLE/MULTI wake 경계를 유지한다.
2. 참조가 있으면 안전하게 공유 PV 루트에 결합하고 manifest 크기/checksum을 검증한다.
3. 이벤트/REST 참조와 manifest의 Execution/Operation/Step/Attempt 및 complete를 대조한다.
   이벤트 참조는 경량형이므로 fence는 manifest에 있으며 REST 상세에서는 추가로 제공한다.
4. manifest가 지정한 source와 representation 파일을 개별 무결성 검증 후 읽는다.
5. false인 결과는 "부분 결과"로 표시하고 status/error와 함께 Agent에 전달한다.
6. null이면 내부 경로를 추측하지 않는다. 필요하면 diagnostics API/운영 로그로 조사한다.

어떤 출력이 불완전하다고 자동 재실행하지 않는다. 코드의 부수효과는 이미 발생했을 수
있다. Runtime이 버린 출력 복원, 실행 중 `.partial` 읽기, 집계 결과 파일 생성,
MCP 기능 추가, DB 실패 후 참조 자동 재등록은 이번 범위가 아니다.

## 개발 계약 전환 주의

요청에 따라 개발 계약 버전은 `1.0`으로 유지한다. 그렇다고 기존 이벤트와 무조건
호환된다는 뜻은 아니다. **complete가 없는 과거 result_ref를 새 strict validator는
거부하며 true로 추정하는 호환 코드를 추가하지 않았다.**

- 기존 실행/Outbox를 구 버전에서 종료·발행·소비한 후 Executor와 소비자를 함께 전환한다.
- 구 버전의 미발행 참조 이벤트가 남으면 새 Publisher의 검증에 실패해 그 Execution의
  뒤 순번 발행도 대기할 수 있다. 전환 전에 pending 확인이 필요하다.
- 오래된 REST 이벤트 이력은 저장된 JSON 그대로다. 새 형식으로 자동 변환하지 않는다.
  새 strict 소비자로 과거 이력을 다시 읽으려면 별도의 데이터 전환 계획이 필요하다.
- 초기 개발 테스트는 새 Execution 범위로 진행한다. 과거 데이터 삭제/변환이 필요하면
  별도 승인 후 수행하며 이 작업에서는 기존 DB·Redis를 수정/초기화하지 않았다.

기존 API 응답에 사용하던 result_ref는 이미 complete가 있으므로 필드 추가는 이벤트만
해당한다. 실제 가동 중인 Executor Docker 이미지는 자동 재빌드/재배포하지 않았다.

## 검증

- 일반 회귀: 413개 통과. 22개 실행/저장 실패 행렬과 12개 출력 제한 케이스에 이벤트,
  Step/StepAttempt, Execution/Operation result, manifest 무결성 대조를 추가했다.
- 취소: SINGLE/MULTI × deadline 유무 × 정상/봉인 실패/DB 참조 저장 실패/저장 timeout
  16개 케이스. 후속 미실행 Step은 참조를 만들지 않고 취소 상태를 보존한다.
- PostgreSQL/Redis: 34개 통과. 두 모드에서 실제 Outbox 발행 → Redis 역직렬화 → DB
  이벤트 이력이 동일하며, 부분 참조가 남고 원문 출력은 이벤트에 없는 것을 검증했다.
- PostgreSQL takeover 테스트는 이전 lease의 취소 참조 쓰기도 거부함을 확인한다.
- Ruff/format/ty 통과.

### 실제 Jupyter 검증 — 2026-08-31

`scripts/jupyter_output_completeness_smoke.py`로 basic(Python 3.11)/ml(Python 3.12)
각 11개, **총 22개 통과**했다. 정상 텍스트·PNG, 사용자 경고 출력, 실제 IOPub 제한,
PNG 생성 뒤 코드 오류/10초 timeout, MULTI 추가 Operation/finalize와 백그라운드
대기 만료 정리를 포함한다. 테스트 중 Jupyter 출력 제한은 올리지 않았다.

- 코드 오류는 `FAILED + complete=true`, timeout은 `FAILED + complete=false`이고
  실제 이미지와 텍스트는 manifest와 노트북에서 같은 내용으로 보존됐다.
- MULTI 코드 오류/timeout 이후 성공한 보정 Operation을 추가하고 finalize했다.
  최종 Execution은 SUCCEEDED여도 앞선 실패 Step와 그 원래 참조/완전성은 유지된다.
  과거 실패를 성공으로 덮어쓰는 처리가 아니다.
- 이벤트 참조의 경로·크기·checksum·complete와 REST/manifest를 대조했다.
- 사용자 취소 및 그 DB 실패 경로는 위 16개 실패 주입 회귀로 검증했다.
  이 22개 실제 Jupyter 사례에 사용자 취소가 포함되었다고 주장하지 않는다.

대표 timeout 증거:

| 환경 | SINGLE timeout Execution | MULTI timeout 후 보정/finalize Execution |
|---|---|---|
| basic | `0dd051f3-5c7b-421c-bae5-7452bbaa004e` | `82fd761c-dfcb-4a75-88cd-e17cdd76722a` |
| ml | `ab1b7700-ebcb-4369-bc0f-48ac92d1a8ff` | `2c6f6098-f4ac-4dea-b71e-5489954fffb9` |

basic SINGLE 노트북 상대경로:

```text
users/diagnostics-smoke/projects/output-completeness/sessions/773f892b-dc46-4f75-bba7-9465063dab85/executions/0dd051f3-5c7b-421c-bae5-7452bbaa004e/notebooks/execution.ipynb
```

검증 DB는 임시 DB, Redis Stream은 UUID 격리 이름을 사용한다. 테스트가 생성한 임시
DB/Stream만 정리한다. 실제 Jupyter 테스트의 노트북은 Jupyter PV에 남고 자기 커널만
정리한다. 임시 공유 결과/DB가 종료 시 사라지므로 해당 테스트 Execution은 기존 서비스
API에서 조회할 수 없다. 프로세스 강제 종료 모든 시점이나 장기 soak 검증은 아니다.

후속 Phase 7에서는 실제 사용자 취소·SIGTERM을 별도 Docker 스택의 8조합으로
검증했다. 부분 출력 참조는 일치했으나 노트북의 이전 성공 표시가 남는 문제를
발견해 상태·진단을 보완했다. 범위와 재현 방법은
[Docker 중단 검증](docker-interrupted-result-validation.md)을 참고한다.
