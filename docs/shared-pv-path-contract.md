# 공유 PV 루트 기준 경로 규칙

## 확정 규칙

Agent/Executor 파일 참조는 **용도별로 설정한 루트 기준 상대경로**다.
마운트 절대경로는 환경설정에만 두며 요청·응답에 서버별 절대경로를 넣지 않는다.

- `INPUT_STORAGE_ROOT`: Agent가 작성한 입력 파일을 Executor가 읽는 루트.
- `SHARED_STORAGE_ROOT`: Executor가 코드 snapshot·출력·manifest를 저장하는 루트.
- 입력 루트 미지정/빈 값이면 결과 루트를 함께 사용한다. 명시적으로 분리했을 때는
  다른 루트까지 검색하지 않는다. Agent 코드 변경은 필요 없으며 결과 reader의
  루트가 Executor 결과 디렉토리를 가리키도록 설정해야 한다.

| 필드 | 기준 |
| --- | --- |
| 실행 제출 및 MULTI Operation의 `payload.source.path` | 입력 루트 (`INPUT_STORAGE_ROOT`) |
| Artifact 생성의 `source.path` | 입력 루트 (`INPUT_STORAGE_ROOT`) |
| API/이벤트의 `result_ref.relative_path` | 결과 루트 (`SHARED_STORAGE_ROOT`) |
| Step manifest의 `source.relative_path` | 결과 루트 (Agent 원본이 아닌 Executor snapshot) |
| Step manifest의 `outputs[].representations[].relative_path` | 결과 루트 |

입력 파일의 하위 디렉토리는 Agent가 선택한다. 예를 들어 Agent가
`/mnt/data/agent/my-code/task-100.py`를 만들고 Executor의 입력 루트가
`/mnt/data/agent`이면 `path: "my-code/task-100.py"`를 보낸다.
Executor는 `/mnt/data/agent/my-code/task-100.py`를 읽으며 결과 루트가
`/mnt/data/executor`여도 입력 경로에 영향을 주지 않는다. `requests/`를 자동 추가하거나
그 하위로 제한하지 않는다. `requests/`라는 폴더를 선택하는 것은 허용되지만,
이 경우 요청에도 `requests/task-100.py`를 온전히 포함해야 한다.

매니페스트에 기록된 출력 경로는 예를 들어
`executions/<execution>/operations/<operation>/steps/<step>/attempts/<attempt>/<fence>/outputs/000000-stream-00.txt`
형태다. Agent는 이 문자열을 그대로 사용하며 ID들로 경로를 재구성하지 않는다.

```python
manifest_path = executor_result_root / result_ref["relative_path"]
source_path = executor_result_root / manifest["source"]["relative_path"]
output_path = executor_result_root / representation["relative_path"]
```

위 코드는 계산 원리만 보여준다. 실제 reader는 절대경로·PV 이탈·symlink 이탈을
차단하고, 결과 파일의 크기/체크섬과 참조 identity를 검증해야 한다. 출력은 자신의
Step/Attempt/fence 결과 디렉토리 내부여야 한다. 코드 입력의 `.py`/UTF-8/크기/SHA-256
검증과 Artifact 텍스트 입력 검증도 그대로 유지한다.

## 변경되지 않는 것

- 실제 코드 snapshot과 출력 파일 저장 위치, `.partial` 봉인 및 원자적 rename.
- 결과 API/Redis의 manifest 참조 및 이벤트 envelope 필드.
- Jupyter PV의 노트북·아티팩트 저장 방식과 Runtime별 다운로드 경로.
- PostgreSQL 테이블/Alembic revision. 개발 중 계약 버전은 `1.0`을 유지한다.

Jupyter PV는 별도 저장소다. Jupyter의 runtime-relative 경로를 Agent/Executor 입력·결과
루트에 붙이면 안 된다.

## 분리 설정 적용 (2026-09-10)

```text
INPUT_STORAGE_ROOT=/mnt/data/agent
SHARED_STORAGE_ROOT=/mnt/data/executor
```

Executor에서 두 경로가 모두 보여야 한다. 입력은 읽기 전용 마운트도 가능하고,
결과는 쓰기 권한이 필요하다. 같은 PVC라도 각 서비스가 `subPath`로 자기 영역만
마운트했다면 상대 영역을 읽을 별도 마운트가 필요하다. `../agent`로 경계를 우회하지 않는다.
입력 디렉토리는 호출자/운영자가 준비하며 Executor가 생성하거나 수정하지 않는다.
기존 결과 루트를 이동할 경우 기존 파일도 같은 상대경로로 접근 가능해야 한다.
이 변경은 기존 파일·DB·Redis를 이동/삭제하지 않으며 schema version도 변경하지 않는다.

분리 환경의 로컬 smoke는 `LOCAL_TEST_INPUT_STORAGE_ROOT`를 Agent 입력 경로로,
`LOCAL_TEST_SHARED_STORAGE_ROOT`를 Executor 결과 경로로 지정한다.
`scripts/path_execution_spec_smoke.py`, `scripts/source_report_matrix_smoke.py`가 이를 지원한다.

분리 루트 검증 기록:

- 전체 회귀: 751 passed, 63 skipped. Ruff/ty 및 격리 PostgreSQL Alembic check 통과.
- 임시 PostgreSQL DB, Redis 6.0.8 고유 스트림, 별도 Executor 프로세스와 실제 Jupyter로
  MCP PATH 실행, INLINE/PATH 리포트 및 MULTI 후속 실행·실패 후 진행·취소를 확인했다.
- 분리한 `agent/` 입력 루트에 Executor 결과가 생성되지 않았고, `executor/` 아래의
  manifest 8개에서 source/output 상대경로·크기·SHA-256이 일치했다.
- 잘못된 루트의 동명 파일, 절대경로, 상위 경로 및 symlink 이탈은 거부되는지 확인했다.
- 기존 서비스 DB/Redis는 초기화하지 않았다. 검증용 DB·스트림·컨테이너만 정리했다.

## 이전 경로 계약 전환 주의사항 (단일 루트 상대경로 정리 당시)

경로 해석이 변경되는 개발 단계의 계약 변경이다. Executor writer/reader와 Agent reader,
PATH 요청을 만드는 호출자를 함께 전환한다. 이전 규칙을 추측하는 fallback은 두지 않는다.

- 기존 입력 파일이 `<PV>/requests/a.py`라면 새 요청은 `path: "requests/a.py"`다.
  새 Agent가 다른 폴더에 작성했다면 그 실제 루트 상대경로를 전달한다.
- 이전 manifest의 `outputs/...`는 새 reader와 호환되지 않는다. 이번 변경으로 기존
  manifest/DB/checksum을 자동 수정하거나 파일을 삭제하지 않는다. 새 계약으로 결과를
  다시 생성해 테스트한다. 기존 결과 보존이 필요하면 별도 데이터 전환을 먼저 설계한다.
- `.state.json`에 이전 경로가 들어 있는 진행 중 작업도 새 writer와 섞지 않는다.
  진행 중 작업을 기존 버전에서 마무리/정리한 뒤 함께 배포한다.

## 검증 기록

- 전체 회귀 최종 실행: 712 passed, 63 skipped. 선택 실행이 필요한 외부 환경
  테스트는 기본 실행에서 건너뛴다.
- 테스트 Agent: 43 passed. 다른 마운트 위치에서 텍스트·이미지 읽기,
  PV/Step 범위 이탈 차단과 이전 경로 형식의 fallback 미사용을 확인했다.
- PostgreSQL/Redis 호환성 테스트: 22 passed. 임시 스키마의 마이그레이션 및
  Alembic check를 포함한다. 기존 서비스 DB는 변경하지 않았다.
- 별도 테스트 프로세스와 실제 로컬 Jupyter로 PATH 실행, INLINE/PATH 리포트
  생성, SINGLE/MULTI 텍스트·이미지 출력과 노트북 반영을 확인했다.
- Ruff lint/format 및 ty 검사를 통과했다.

### 별도 후속 점검: SQLite 동시성 테스트의 간헐적 실패

`test_worker_pool_concurrency.py`의
`test_worker_does_not_apply_a_process_local_execution_limit[BATCH-INTERACTIVE-3]`
항목은 최초 전체 실행 및 반복 실행 중 실패했다. 수정 코드에서는 이벤트 순번의
UNIQUE 충돌이 관찰됐고, 변경 전 기준 커밋 `6cb1828`을 임시 디렉토리에서 실행해도
동일 테스트가 실패했다(완료 이벤트에 RUNNING 상태 전달).

해당 테스트는 메모리 SQLite 연결을 여러 비동기 실행이 공유한다. 연결/트랜잭션
격리와 실패 시 비동기 작업 정리를 별도 조사해야 한다. 기존 코드에서도 실패하므로
이번 경로 변경만으로 생긴 현상으로 단정하지 않으며, 전체 재실행 통과만으로 문제가
해결됐다고 간주하지 않는다. 이번 변경에는 관련 Worker/DB 로직 수정을 포함하지 않는다.
