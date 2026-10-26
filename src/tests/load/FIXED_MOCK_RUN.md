# 고정 mock 시나리오 실행

## 바뀐 점

`src/app/agents/orchestration/mock_workflow_scenario.json` 하나에 질문, Workflow 1.0,
필드 26개의 고정 값/출력 연결을 정의했다. API worker의 mock agent와 부하 클라이언트가
같은 파일을 읽는다. 다섯 개 입력값은 data_type=nce, lot_cd=LOT1, process=[P01],
start_dt=2026-01-01, end_dt=2026-02-01이다. 나머지는 정해진 선택값과 도구 출력 연결이다.

흐름: NCE 데이터 품질을 확인해줘 → 고정 분석 분류 → 고정 skill/workflow → 후보 선택 →
고정 필드 filling → 승인 → Executor 제출 → EXECUTOR_EVENT 대기.
매 요청의 task/run/interaction ID는 서버에서 발급한 값을 사용한다.

실제 LLM을 생성하지 않고 `_FixedAgent`가 고정 응답을 반환한다. 추천기도 고정된
추천 없음 응답을 사용한다. mock 응답은 매 호출 복사하여 사용자 간 객체 변경을 격리한다.
그래프, DB, queue, HITL 검증, 컴파일, Executor 제출 노드는 실제로 실행한다.
예상과 다른 폼/순서는 즉시 실패하며 LLM으로 재시도하지 않는다.

## 1. 서버 코드 반영

이번 기능에 필요한 파일:

- src/app/agents/orchestration/mock_llm.py (수정)
- src/app/agents/orchestration/mock_workflow_scenario.json (추가, 반드시 이미지에 포함)
- scripts/mock_executor_server.py (수정: 신규 execution_id를 UUID로 반환)

부하 실행 쪽에도 `src/tests/load/run_workflow_load.py`와 동일한 scenario JSON을 반영한다.
이전에 추가한 compiler/generate_workflow 진단 정보는 고정 시나리오의 동작 조건이 아니다.

## 2. mock Executor 실행

worker에서 접근할 수 있는 호스트/컨테이너에서:

```bash
python scripts/mock_executor_server.py --host 0.0.0.0 --port 8080 --no-save
```

API와 worker의 환경변수를 다음과 같이 설정하고 기동/재시작한다.

```text
MOCK_LLM=1
MOCK_LLM_SLEEP_SECONDS=0
EXECUTOR_SUBMIT_ENABLED=true
EXECUTOR_BASE_URL=http://<mock-executor의-worker에서-접근가능한-주소>:8080
```

예를 들어 같은 컨테이너에서 실행했다면 http://127.0.0.1:8080,
별도 서비스라면 해당 서비스 DNS를 사용한다. 원격 worker의 localhost는 로컬 PC가 아니다.
기존 Executor의 경로 설정은 mock 서버가 모든 POST 경로를 수용하므로 유지해도 된다.

**Executor까지 가는 이번 시나리오는 EXECUTOR_SUBMIT_ENABLED=true로 실행한다.**
이전 설명의 false는 외부 제출 생략 설정이고 Executor 도달 검증에 쓰는 설정이 아니다.
실제 Executor를 검증하려면 EXECUTOR_BASE_URL을 실제 서비스로 설정할 수 있으나,
그 경우 실제 데이터/분석 실행은 이 LLM mock과 별개로 발생할 수 있다.

## 3. 부하 클라이언트 실행

프로젝트 루트에서:

```powershell
python src/tests/load/run_workflow_load.py --users 1 --rounds 1
python src/tests/load/run_workflow_load.py
```

첫 명령은 smoke 1건, 두 번째는 1/5/10/20/50/100/150명 각 5회다.
기본 URL은 https://gaia-prj-03-0042-v2-api-basic-dev.api.hcpnd01.skhynix.com.
노드별 시간과 worker 대기는 var/workflow-load-.../node_timings.csv, queue_wait.csv에 저장한다.

## 검증과 측정 범위

로컬 실제 LangGraph에 부하 클라이언트의 입력 로직을 연결하고, 실제 LLM 생성은 실패하도록
차단한 상태에서 mock Executor 202 응답 이후 EXECUTOR_EVENT 대기까지 검증했다.
DB/Redis/사내 HTTP 배포 전체는 이 로컬 테스트에 포함되지 않는다.
서버의 Executor 요청 제출까지가 측정 범위다. mock Executor는 분석 코드를 실행하지 않고
완료 이벤트도 발행하지 않으므로 서버의 task는 Executor 대기 상태로 남는다.
테스트 계정/task/run/대기 binding 정리는 별도로 수행해야 한다.

테스트 실행:

```powershell
cd src
python -m unittest app.test.test_workflow_first_scenario.WorkflowFirstTests.test_fixed_load_client_reaches_executor_without_llm app.test.test_mock_generation_concurrency -v
python -m unittest discover -s tests/load -p test_workflow_load.py -v
```
