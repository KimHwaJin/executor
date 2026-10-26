# demo_v2 workflow_run 부하 테스트

> 고정 질문/필드/응답으로 Executor까지 도달하는 현재 실행 절차는
> [FIXED_MOCK_RUN.md](FIXED_MOCK_RUN.md)를 따른다. 이 시나리오는 mock Executor에
> `EXECUTOR_SUBMIT_ENABLED=true`로 제출한다. 아래 false 설정은 제출 생략용이다.

Python 3.10 이상, 외부 패키지 설치 불필요. 서버는 별도로 실행한다.
브라우저 자동화가 아니라 demo_v2.html의 REST 제출 경로를 호출한다.
`POST /api/v1/workflow_run` → `GET /api/v1/workflow_run/{run_id}`로 상태를 조회하며
후보 선택 → tool 입력값 제출 → 승인 → Executor 대기 또는 success까지 진행한다.
프런트의 SSE 연결 부하는 포함하지 않는다.

## 서버 준비

확인한 소스에는 이미 다음 기능이 구현되어 있어 서버 코드 추가는 필요 없다.

- `src/app/api/v1/routes/workflow_run.py`: `X-DTest-Mock-LLM: 1` 요청을 run metadata에 저장.
- `src/app/services/run_service.py`, `agent_graph_service.py`: worker에서 mock graph 선택.
- `src/app/agents/orchestration/mock_llm.py`: 고정된 LLM 응답과 workflow 데이터.
- `src/app/services/graph_crud_persistence.py`: 노드 계측값 저장.
- `src/app/schemas/common/run_schema.py`: node_timings와 queue_wait_seconds 응답.

**위 코드가 API와 worker 모두에 배포되어 있어야 한다.** 헤더만 지원하지 않는 서버에
전송한다고 LLM 호출이 자동 차단되는 것은 아니다. 전용 테스트 배포에서는 API와 worker를
`MOCK_LLM=1`, `MOCK_LLM_SLEEP_SECONDS=0`, `EXECUTOR_SUBMIT_ENABLED=false`로 기동한다.
환경변수 변경 후 프로세스를 다시 시작해야 한다. 스크립트도 모든 요청에 mock 헤더와
`X-DTest-Mock-LLM-Sleep: 0`을 전송한다. `--mock-sleep 5`로 인위적인 LLM 지연을 넣을 수 있다.

LLM mock과 외부 Executor mock은 별개다. `EXECUTOR_SUBMIT_ENABLED=false`는
Executor 요청 파일 생성까지만 하고 실제 외부 제출을 생략한다. 외부 제출 HTTP 비용까지
측정하려면 기존 `scripts/mock_executor_server.py`를 실행하고 서버의 Executor 주소를
해당 mock 서비스로 설정한다. 이 테스트는 실제 분석 완료나 Executor 내부 queue를 측정하지 않는다.
DB, worker, 체크포인트, workflow 컴파일 및 파일 생성은 실제로 동작한다.

## 실행

프로젝트 루트에서 먼저 1명 smoke test:

```powershell
python src/tests/load/run_workflow_load.py --users 1 --rounds 1
```

전체 1, 5, 10, 20, 50, 100, 150명 × 각 5회:

```powershell
python src/tests/load/run_workflow_load.py
```

기본 URL은 `https://gaia-prj-03-0042-v2-api-basic-dev.api.hcpnd01.skhynix.com`이다.
프로토콜이나 API prefix가 다르면 `--host http://... --api-prefix /api/v1`로 변경한다.
사내 DNS/네트워크 및 정상적인 TLS 신뢰 설정이 필요하다.

```powershell
python src/tests/load/run_workflow_load.py --users 10 50 --rounds 3 --out-dir var/custom-run
python src/tests/load/run_workflow_load.py --dry-run
python -m unittest discover -s src/tests/load -p test_workflow_load.py -v
```

`--rounds N`은 각 사용자 수에서 **N회의 독립 회차**를 뜻한다. 회차마다 각 사용자가
workflow 1개를 실행한다. 기본값은 35개 케이스, 총 1,680개 workflow다.
기존 locust 스크립트의 `--repeats 1 2 3 4 5`(사용자마다 1~5개 작업을 실행하는 별도 행렬)와 의미가 다르다.
사용자 계정과 기본 프로젝트 준비가 끝난 후 barrier에서 동시에 workflow 제출을 시작한다.
회차의 모든 사용자가 끝난 후 다음 회차로 넘어간다. 사용자 준비 실패 시 해당 회차의
barrier를 해제하고 실패로 기록하여 불완전한 동시성 결과가 성공으로 남지 않게 한다.

`--tool-values values.json`으로 기본 입력을 덮어쓸 수 있다. 예: `{"lot_cd":"LOT2"}`.
알 수 없는 필수 입력은 임의 생성하지 않고 실패한다. 모든 테스트 계정은 `load-v2-`로
시작하며 계정, task, run, 생성 파일은 보존된다. 실패/timeout은 서버 작업 취소를 뜻하지 않는다.
테스트 후 서버 데이터 정리는 별도로 수행한다.

## 결과

기본 출력 폴더: `var/workflow-load-날짜-고유값/`. 기존 폴더에는 덮어쓰지 않는다.

| 파일 | 내용 |
|---|---|
| config.json | 실행 URL, 사용자 수, 회차, timeout, mock 입력 설정 |
| samples.jsonl | 사용자별 전체 결과, 모든 run의 응답, HTTP 상태/응답 시간/오류 |
| queue_wait.csv | 사용자/회차/task/run별 worker 대기 시간, created_at, started_at |
| node_timings.csv | 사용자/회차/task/run/node별 wall_seconds, count, min/max |
| summary.csv | 케이스별 성공/실패, workflow 시간 평균/p95, run queue 대기 평균/p95 |

시간 단위는 초다. queue 대기는 서버의 `started_at - created_at`으로, run 생성부터
worker 시작까지의 구간이다. 재시도/스케줄링 지연이 있다면 포함되며, 순수 broker 대기나
Executor 대기와 다르다. 노드값은 서버가 제공하는 callback 계측 그대로이며, 하나의 노드에
여러 callback이 집계될 수 있어 count가 업무 노드 방문 횟수와 반드시 같지는 않다.
`wall_seconds / count`로 해당 bucket의 평균을 구할 수 있고, 중첩 구간 때문에 노드 합계가
전체 경과 시간과 같다는 보장은 없다.

계측 누락은 0초로 바꾸지 않는다. 최대 5초 동안 재조회한 후 실패로 기록한다.
성공 요약에는 성공한 workflow만 포함되며, 실패 시 확보한 계측값은 원시 CSV/JSONL에 남는다.
queue 요약은 workflow 누적합이 아닌 **개별 run별** 평균/p95다.
workflow 경과 시간은 계정 준비 이후부터 측정하고, HTTP 및 polling 지연을 포함한다.
이 클라이언트는 Python thread와 urllib를 사용하므로 브라우저의 연결 재사용 특성과는 다르다.
기본 polling 주기는 0.5초로 최대 150명일 때 status 조회 부하도 함께 발생한다.

실패가 한 건이라도 있으면 종료 코드 1, 모두 성공하면 0이다. raw JSONL은 사용자 완료마다,
CSV는 회차 완료마다 저장한다. 강제 종료 시 미완료 사용자 결과는 없을 수 있다.
로컬 가짜 HTTP 서버를 이용하는 계약 테스트는 포함되어 있으며 실제 사내 서버의 배포 상태와
성능은 별도로 검증해야 한다.
