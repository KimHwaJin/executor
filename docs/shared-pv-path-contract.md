# 공유 PV 루트 기준 경로 규칙

## 확정 규칙

Agent/Executor 공유 PV의 모든 파일 참조는 **각 서비스가 마운트한 공유 PV 루트
기준 상대경로**다. 마운트 절대경로는 환경설정에만 둔다.

| 필드 | 기준 |
| --- | --- |
| 실행 제출 및 MULTI Operation의 `payload.source.path` | 공유 PV 루트 |
| Artifact 생성의 `source.path` | 공유 PV 루트 |
| API/이벤트의 `result_ref.relative_path` | 공유 PV 루트 |
| Step manifest의 `source.relative_path` | 공유 PV 루트 |
| Step manifest의 `outputs[].representations[].relative_path` | 공유 PV 루트 |

입력 파일의 하위 디렉토리는 Agent가 선택한다. 예를 들어 Agent가
`/agent/shared/my-code/task-100.py`를 만들고 PV를 `/agent/shared`에 마운트했다면
`path: "my-code/task-100.py"`를 보낸다. Executor의 마운트가 `/workspace/shared`라면
`/workspace/shared/my-code/task-100.py`를 읽는다. `requests/`를 자동 추가하거나
그 하위로 제한하지 않는다. `requests/`라는 폴더를 선택하는 것은 허용되지만,
이 경우 요청에도 `requests/task-100.py`를 온전히 포함해야 한다.

매니페스트에 기록된 출력 경로는 예를 들어
`executions/<execution>/operations/<operation>/steps/<step>/attempts/<attempt>/<fence>/outputs/000000-stream-00.txt`
형태다. Agent는 이 문자열을 그대로 사용하며 ID들로 경로를 재구성하지 않는다.

```python
manifest_path = shared_root / result_ref["relative_path"]
source_path = shared_root / manifest["source"]["relative_path"]
output_path = shared_root / representation["relative_path"]
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

Jupyter PV는 별도 저장소다. Jupyter의 runtime-relative 경로를 Agent/Executor 공유
PV 루트에 붙이면 안 된다.

## 전환 주의사항

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
