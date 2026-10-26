# dtest-agent
dtest 프로젝트 공유

## LLM 비동기 호출

라우팅, 분석 의도 분류, FAQ, 스킬 선택, 워크플로우 생성, 조건부 판단,
보고서 생성은 `await agent.ainvoke()`로 모델을 호출합니다. 응답 검증과
재시도 규칙, HITL 및 REST/SSE 규격은 유지합니다.

그래프를 직접 호출할 때는 `await graph.ainvoke(...)` 또는
`async for ... in graph.astream(...)`을 사용합니다. LLM 노드는 이제 async
함수이므로 그래프의 동기 `invoke/stream` 호출은 지원하지 않습니다.
주입하는 사용자 정의 에이전트도 `ainvoke`를 구현해야 합니다.
개별 어댑터의 기존 `invoke`는 유지합니다.

CLI는 실행부터 HITL 재개까지 한 이벤트 루프를 사용하며,
`--postgres`에서는 `AsyncPostgresSaver`를 사용합니다.
Mock LLM 대기도 `asyncio.sleep`을 사용합니다.
Executor HTTP와 워크플로우 DB 저장도 비동기로 호출합니다.
파일 작업과 기존 방식으로 주입한 동기 콜백은 별도 스레드로 분리합니다.
이러한 스레드 작업의 실행 중 취소까지 보장하는 변경은 아닙니다.

외부 LLM 서버 없이 비동기 SDK 호출·동시 대기·취소 전달을 확인할 수 있습니다.
프로젝트 Python 환경을 활성화한 PowerShell에서 실행하세요.

```powershell
$env:PYTHONPATH = "src;."
$env:PYTHONUTF8 = "1"
python -P -m unittest app.test.test_async_llm app.test.test_workflow_chat_stream
```

SDK 테스트는 실제 LangChain/OpenAI 비동기 클라이언트와 가짜 HTTP 응답을
사용합니다. 실제 모델의 응답 속도 개선율을 측정하는 테스트는 아닙니다.

## Executor HTTP와 워크플로우 DB 연결

Executor 시작·계속·종료·취소·보고서 전송 및 결과·노트북 조회는
`httpx.AsyncClient`를 사용합니다. 이벤트 루프마다 클라이언트를 재사용하며,
기존 `EXECUTOR_TIMEOUT_SECONDS`를 연결·읽기·쓰기·풀 대기 시간에 적용합니다.
POST를 자동 재시도하지 않으며 기존 idempotency key와 응답 구조를 유지합니다.
HTTP 취소가 Executor 서버에서 이미 시작한 분석까지 취소하는 것은 아닙니다.

워크플로우 저장은 `psycopg_pool.AsyncConnectionPool`을 사용합니다.
같은 이벤트 루프와 DB URL에서는 그래프를 새로 만들어도 풀을 재사용합니다.
각 저장 작업은 연결을 빌려 트랜잭션을 수행한 뒤 반환하며, 기존 테이블·SQL·
중복 저장 처리·완료 횟수 집계 규칙을 유지하므로 DB migration은 필요 없습니다.

DB 주소는 기존 `WORKFLOW_DATABASE_URL`, 없으면 `EW_DATABASE_URL`을 사용합니다.
다음 설정은 선택 사항입니다.

| 환경변수 | 기본값 | 의미 |
|---|---:|---|
| `WORKFLOW_DB_POOL_MAX_SIZE` | 8 | 이벤트 루프·DB URL별 최대 연결 수 |
| `WORKFLOW_DB_POOL_TIMEOUT_SECONDS` | 10 | 풀 연결을 빌리기 위한 최대 대기 시간(초) |
| `WORKFLOW_DB_CONNECT_TIMEOUT_SECONDS` | 10 | DB 연결 시도 제한 시간(초) |
| `WORKFLOW_DB_STATEMENT_TIMEOUT_MS` | 30000 | SQL 실행 제한 시간(ms) |

API, LangGraph dev, Worker, CLI 종료 시 활성 그래프 작업이 끝난 뒤
HTTP 클라이언트와 DB 풀을 닫습니다. 별도 스크립트에서 직접 사용하는 경우에는
같은 이벤트 루프의 `finally`에서
`await close_async_io_resources()`를 호출하세요
(`app.services.async_io_resources`). 연결 풀은 프로세스 사이에서 공유하지 않습니다.

HTTP 및 저장 함수, 결과 조회 함수와 선택형 폴링의 `wait()`는 이제 async 함수입니다.
직접 호출하는 코드는 `await`를 사용해야 합니다. 공유 경로의 MANIFEST 읽기는
동기 파일 작업이므로 스레드에서 실행합니다.

```powershell
python -P -m unittest app.test.test_async_executor_storage app.test.test_execution_results app.test.test_executor_submit_toggle app.test.test_workflow_persistence
```

위 테스트는 가짜 HTTP transport와 DB 연결로 오류·취소·트랜잭션 경계·연결 정리를
확인합니다. 실제 Executor/PostgreSQL 연동 및 부하 측정은 별도 검증이 필요합니다.

## CRUD API와 테스트 화면

LangGraph/노드 구현과 분리된 FastAPI 계층에서 사용자, 프로젝트, 세션, 메시지,
실행 이력을 PostgreSQL에 저장합니다. CRUD 스키마는 기존 Worker migration과 별도로
관리합니다.

```bash
uv sync
alembic -c alembic.crud.ini upgrade head
uv run dtest-agent-api
```

브라우저에서 `http://127.0.0.1:8000/demo`를 열면 CRUD와 Agent 실행 흐름을 시험할 수
있습니다. DB/Redis/checkpoint 연결은 `DATABASE_URL`, `REDIS_URL`,
`CHECKPOINT_DB_URI` 및 기존 `EW_*` 환경변수로 덮어쓸 수 있습니다.

## Local Redis event worker

Executor 제출 후 LangGraph run은 interrupt 상태로 종료됩니다. Redis event
worker가 Executor 완료 이벤트를 소비하고 같은 thread/checkpoint를 resume합니다.
로컬 테스트할 때는 migration이 적용된 PostgreSQL과 Redis가 필요하며,
두 터미널에서 각각 실행합니다.

```bash
langgraph dev
```

```bash
set -a
source .env
set +a
PYTHONPATH=src ../.venv311/bin/python -m app.agent_worker.worker_main
```

## Workflow Run API

신규 실행·재개 REST/SSE 및 상태 구독 계약은 [Workflow Run API](docs/workflow_run_api.md)를 참고하세요.

채팅 테스트 화면은 `http://127.0.0.1:8000/demo_temp.html`입니다. 기존 `/demo` 상단에서도 이동할 수 있습니다. X-User-ID로 로그인하고, REST + GET SSE 또는 POST SSE 방식으로 workflow_run 실행·재개를 테스트합니다. 사이드바에서 SSE 끊기·재연결과 동일 Session 동시 요청을 실행하고, Run/Lock 탭에서 1초마다 갱신되는 Heartbeat·Lease를 확인할 수 있습니다. Lease 만료 복구는 실행 중 Worker를 외부에서 중지한 뒤 관찰합니다.
