# 공유 PV 루트 기준 경로 규칙

## 규칙

Batch·Agent·Executor는 같은 PVC를 같은 위치에 마운트하고, 공통 프로젝트 루트
(`STORAGE_ROOT`, 예: `/mnt/data/dtest`)를 함께 쓴다. 서비스 사이에 주고받는 파일 경로는
**모두 이 루트 기준 상대경로**이고, 첫 폴더가 그 파일을 쓴 서비스의 영역이다.
마운트 절대경로는 환경설정에만 두며 요청·응답에 절대경로를 넣지 않는다.

```text
<STORAGE_ROOT>/
├── batch/...                 # Batch가 쓰는 입력
├── agent/...                 # Agent가 쓰는 입력
└── executor/executions/...   # Executor가 쓰는 snapshot·출력·manifest
```

| 필드 | 형태 |
| --- | --- |
| 실행 제출 및 MULTI Operation의 `payload.source.path` | 호출자 영역 경로 (예: `agent/tasks/100/step.py`) |
| Artifact 생성의 `source.path` | 호출자 영역 경로 |
| API/이벤트의 `result_ref.relative_path` | `executor/executions/.../manifest.json` |
| Step manifest의 `source.relative_path` | `executor/executions/.../source.py` (Executor snapshot) |
| Step manifest의 `outputs[].representations[].relative_path` | `executor/executions/.../outputs/...` |

Executor는 입력 경로에 `requests/` 같은 접두어를 붙이지 않고, 루트 밖(절대경로,
`..`, symlink 이탈)은 거부한다. 결과 폴더 `executor/executions`는 코드에 고정된 이름이며
설정하지 않는다. Executor가 읽는 결과 참조도 이 폴더 아래로 제한된다.

호출자는 경로 문자열을 그대로 사용하며 ID들로 경로를 재구성하지 않는다.

```python
manifest_path = storage_root / result_ref["relative_path"]
source_path = storage_root / manifest["source"]["relative_path"]
output_path = storage_root / representation["relative_path"]
```

위 코드는 계산 원리만 보여준다. 실제 reader는 절대경로·PV 이탈·symlink 이탈을
차단하고, 결과 파일의 크기/체크섬과 참조 identity를 검증해야 한다. 출력은 자신의
Step/Attempt/fence 결과 디렉토리 내부여야 한다.

## 마운트

모든 서비스가 PVC 최상위를 같은 경로(예: `/mnt/data`)에 마운트하거나, 같은
`subPath`로 같은 폴더를 같은 경로에 마운트해야 한다. 경로 문자열이 같아도 마운트
위치가 다르면 서로 다른 폴더를 가리킨다. 예를 들어 Executor는 PVC를 `/mnt/data`에,
Agent는 `/mnt/data/dtest`에 마운트하면 Agent의 `/mnt/data/dtest/agent/a.py`는
Executor에서 `/mnt/data/agent/a.py`가 되어 읽히지 않는다.

Jupyter PV는 별도 저장소다. Jupyter의 runtime-relative 경로를 공유 루트에 붙이면 안 된다.

로컬 smoke는 `LOCAL_TEST_STORAGE_ROOT`로 루트를 지정한다
(`scripts/path_execution_spec_smoke.py`, `scripts/source_report_matrix_smoke.py`).

## 전환 (2026-11-02)

`SHARED_STORAGE_ROOT`(결과)와 `INPUT_STORAGE_ROOT`(입력)를 `STORAGE_ROOT` 하나로
합쳤다. 결과 참조는 `executions/...`에서 `executor/executions/...`로 바뀌었다.
개발 단계 변경이라 이전 형식 fallback과 데이터 전환은 두지 않는다. 이전 결과는 새 버전에서
읽지 않으므로 결과를 다시 생성해 테스트한다.

## 참고

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
