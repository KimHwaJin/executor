# tenant 기반 Executor 이벤트 라우팅 인수인계

브랜치: `feature/tenant-event-routing` (기준: `feature/total_merge_v1` bf6c58a, 로컬 전용)

## 1. 목적

Executor는 `tenant_id`와 함께 제출된 Execution의 이벤트를 `executor.events.{tenant_id}`
Stream으로만 발행한다. `tenant_id`는 **이벤트를 받아 처리할 서비스**의 ID이고, 서비스 하나에
tenant 하나를 쓴다(pr-batch 요청서와 같은 원칙).

dtest-agent의 tenant는 **`dtest` 하나**다(`EXECUTOR_TENANT_ID`). 이 값은 다음 세 곳에 똑같이 쓰인다.

- 분석 에이전트가 Executor에 제출할 때의 `tenant_id`
- pr-batch에 배치를 등록할 때의 `tenant_id`
- Worker가 구독하는 유일한 Stream `executor.events.dtest`

같은 tenant 안에서 에이전트 실행과 배치 실행은 `trigger.type`으로 구분한다.

| 에이전트 | Route 종류 | 처리하는 Execution | 받는 이벤트 | 동작 |
|---|---|---|---|---|
| 분석 에이전트(기존) | 등록형 | 분석 그래프가 제출하고 등록한 Execution(INTERACTIVE) | `execution.operation_completed`, `execution.completed` | `interrupt`로 기다리던 그래프를 재개한다(기존과 같음) |
| 리포트 에이전트(골격) | claim형 | pr-batch가 `dtest`로 제출한 Execution(BATCH) | `execution.completed` | 성공한 배치마다 그래프를 새로 실행한다 |

하위 호환(공용 `executor.events` 구독, tenant 없는 실행)은 지원하지 않는다.

## 2. 구조

```text
Executor ──XADD──▶ executor.events.dtest   (분석 에이전트 제출 + pr-batch 배치)
                         │ ingress consumer
                         ▼
          Ingress ① envelope tenant_id == "dtest" 확인(다르면 DLQ)
                  ② binding이 없고 claim 필터가 참이면 Worker가 binding 생성
                     (리포트: execution.completed + trigger.type == BATCH)
                  ③ ew_inbox 저장(event_id 중복 제거)
                         ▼
          EventRouter: binding이 있는 Execution만 순번대로 처리,
                       빠진 순번은 Executor REST 이력으로 복구,
                       (tenant, event_type, 등록형/claim형)으로 Route 조회
                         ▼
          ew_commands / ew_outbox → 내부 command Stream
                         ▼
          Dispatcher: 세션 잠금 → handler
                  ├─ 등록형: LangGraphEventAdapter(분석 그래프 resume)
                  └─ claim형: LangGraphClaimAdapter(리포트 그래프 invoke)
```

### binding (Execution → 처리 주체)

`ew_bindings`에 (tenant, session_id, task_id, last_sequence)가 저장된다.

| 종류 | 누가 만드나 | 언제 | session_id(= LangGraph thread_id) | task_id | 처리 시작 순번 |
|---|---|---|---|---|---|
| 등록형 | 분석 그래프 `register_execution` 노드 | 제출 직후 | 분석 세션 ID | Agent task ID | 1 |
| claim형 | Worker Ingress | claim 필터를 통과한 이벤트를 받을 때 | `"dtest:{execution_id}"` | NULL | claim한 이벤트 |

- binding이 없는 Execution의 이벤트는 Inbox에서 기다린다. 분석 에이전트가 등록하기 전에 이벤트가
  먼저 와도 유실되지 않는다.
- claim은 binding이 **없을 때만** 만든다. 등록형 binding은 절대 덮어쓰지 않는다.
- claim형 binding은 claim한 이벤트부터 처리한다(`last_sequence = 그 순번 - 1`). 배치의 앞선 이벤트
  (`started`, `step_*` 등)는 라우팅하지도 REST에서 가져오지도 않는다. 그래서 앞선 이벤트가 없어도
  (Stream 길이 제한, 구독 전 발행, 보관 정리) 리포트는 정상 동작한다.
- INTERACTIVE Execution은 claim되지 않는다. 분석 에이전트의 등록을 기다린다.
- 등록형/claim형 구분은 `task_id`가 NULL인지로 한다. 등록은 task_id가 비어 있으면 거부하므로,
  등록형 binding이 NULL을 가질 수 없다.

## 3. 에이전트 개발자가 할 일

### 3.1 리포트 에이전트 구현

`src/app/graphs/builders/build_report_graph.py`의 `write_report`는 `NotImplementedError`를 던지는 골격이다.

- 그래프 상태는 `AgentEventInput`을 상속해야 한다(`ReportState`처럼). `ew_command_id`가 checkpoint에
  남아야 재전달 시 중복 실행을 막는다.
- `interrupt`(사람 입력 대기)를 쓰지 않는다. claim형 그래프가 멈춰 있으면 다음 이벤트는 `RejectEvent`가 된다.
- 결과와 상세는 빌더 인자 `executor_api`로 읽는다.
  - `await executor_api.result(execution_id)` → `GET /executions/{id}/result`
  - `await executor_api.execution(execution_id)` → `GET /executions/{id}`(`metadata.batch_id` 등은 여기서만)
- 의존성(LLM 등)은 `build_report_graph` 인자로 추가하고 `graph_provider.build_report_agent_graph`에서 넘긴다.

### 3.2 리포트 에이전트가 받는 입력 (`AgentEventInput`, `app/agent_worker/event_input.py`)

| 키 | 내용 |
|---|---|
| `tenant` | `dtest` |
| `execution_id`, `event_id`, `event_type`, `event_sequence`, `occurred_at` | Executor envelope 값 |
| `status` | `payload.status`. 필터 때문에 리포트에는 `SUCCEEDED`만 온다 |
| `payload` | `execution.completed` payload 원본(`operation_summary`, `error`, `retry`, `execution`) |
| `execution` | `{"trigger": {"type", "actor"}, "context": {"user_id", "task_id", "project_id", "session_id", "workflow_id"}}`. claim 필터가 이 필드가 있는 이벤트만 받으므로 항상 있다 |
| `ew_command_id` | Worker 내부 command ID(멱등 처리용, 수정 금지) |

필터는 두 단계다.
1. claim(`batch_completion`): BATCH의 `execution.completed`일 때만 binding을 만든다.
2. 실행 필터(`succeeded`): `SUCCEEDED`가 아니면 그래프를 호출하지 않고 IGNORED로 기록한다.

### 3.3 Route 추가·수정 (`src/app/agent_worker/worker_hooks.py`)

`build_routes()`가 유일한 등록부다. `Route(tenant, event_types, handler, claim=None)`:

- `claim` 없음: 이 서비스가 제출하고 등록한 Execution(등록형).
- `claim=필터`: 이 서비스가 제출하지 않은 Execution(claim형). 필터는 `ExecutorEvent → bool`인
  순수 함수여야 한다(I/O 금지, 예외 금지). 필터가 참인 이벤트부터 처리된다.
- 같은 tenant·이벤트 유형에 등록형 하나, claim형 하나까지 둘 수 있다.

새 에이전트 추가 순서:
1. `app/graphs/builders/`에 그래프 빌더를 만든다(claim형이면 상태가 `AgentEventInput`을 상속).
2. `graph_provider.py`에 빌드 함수를 추가한다.
3. `worker_hooks.build_routes()`에 Route를 추가한다.
4. `worker_main.py`에서 그래프를 한 번 빌드해 `DeferredHandler.bind(...)`로 연결한다
   (claim형 `LangGraphClaimAdapter(graph, accept=...)`, 등록형 `LangGraphEventAdapter`).
5. on/off는 env 설정으로 둔다.

Route에 없는 이벤트도 순번 정렬을 위해 받아서 IGNORED로 기록한다(`execution.step_handover_*` 포함).

## 4. 오류·재시도

| 상황 | 처리 |
|---|---|
| 정상 종료 | command `DONE` |
| `IgnoreEvent`(필터 불통과 포함) | `IGNORED`, 다음 이벤트 진행 |
| `DeferEvent` | 실패 횟수 증가 없이 나중에 재전달 |
| 일반 예외 | `EW_MAX_HANDLER_ATTEMPTS`(기본 5)까지 재시도 후 `FAILED`, DLQ `{namespace}:dispatch:dlq` |
| `RejectEvent` | 즉시 `FAILED`, DLQ |
| envelope `tenant_id`가 구독 Stream과 다름 | 저장하지 않고 DLQ `{namespace}:ingress.dtest:dlq` |
| claim형 Execution 이벤트인데 이 Pod에 claim Route가 없음(롤아웃 중 등) | `IGNORED`(뒤 이벤트를 막지 않음) |
| 등록형 Execution 이벤트인데 handler가 없음 | `DeferEvent`(다른 버전 Pod가 처리하도록 남김) |

- 같은 Execution의 이벤트는 순서대로 하나씩 처리한다. `FAILED`가 생기면 그 Execution의 뒤 이벤트는
  멈추고, `Store.resolve_failed(retry|skip)`로 푼다.
- claim형 그래프가 실행 중에 끊기면(Pod 종료 등) 같은 command가 다시 전달된다. checkpoint의
  `ew_command_id`가 같으면 끊긴 지점부터 이어서 실행하고, 이미 끝났으면 건너뛴다.

## 5. 설정

| 환경변수 | 대상 | 기본값 | 의미 |
|---|---|---|---|
| `EXECUTOR_TENANT_ID` | API, Worker | 없음(필수) | `dtest`. Executor 형식(소문자로 시작하는 3~64자 등)이 아니거나 `dlq`·`work`면 설정 로드 시 오류. 없으면 API·Worker 모두 시작 시 실패 |
| `AGENT_REPORT_ENABLED` | Worker | `false` | `true`면 성공한 BATCH 실행마다 리포트 에이전트를 실행 |
| `EW_RETENTION_DAYS` | Worker | `14` | 아래 행을 이 일수 뒤에 삭제(10분마다). 0이면 끔 |
| `EW_EXECUTOR_EVENT_STREAM` | Worker | `executor.events` | Executor `REDIS_EVENT_STREAM`과 같은 값. 구독 Stream은 `{값}.{EXECUTOR_TENANT_ID}` |

`EW_RETENTION_DAYS`가 지우는 것:
- `DONE`/`IGNORED` command와 그 outbox·inbox 행
- 라우팅되지 않은 inbox 행(IGNORED), 아무 binding도 없는 inbox 행(아무도 처리하지 않는 실행),
  binding의 처리 시작 순번 이전 행(재전달, claim 전 이벤트)

지우지 않는 것: `FAILED`·`READY`·`RUNNING` command와 그 inbox 행, binding(늦은 재전달이 에이전트를 다시
실행하지 않도록 유지). 14일은 Executor의 Redis Stream 보관 기간(`REDIS_EVENT_RETENTION_SECONDS`, 기본 7일)보다
길어서, Stream에서 다시 전달될 수 있는 이벤트의 중복 제거 행이 먼저 지워지지 않는다. 처리 중인 실행에는 영향이 없다.

## 6. 적용 순서

이벤트를 받는 쪽이 먼저 구독해야 한다. tenant가 실린 실행의 이벤트는 공용 Stream에 복사되지 않는다.

1. **DB 마이그레이션**: `python -m alembic -c alembic.ini upgrade head`(`ew_0007`). `ew_bindings.tenant` 추가,
   `task_id` nullable. 이전에 만들어진 binding(tenant NULL)은 더 이상 라우팅되지 않는다(하위 호환 없음).
   배포의 `worker-migration` initContainer가 실행한다.
2. **배포**: API와 Worker에 `EXECUTOR_TENANT_ID=dtest`(매니페스트에 반영됨). 배포 시점에 공용 Stream으로
   진행 중이던 Execution의 이후 이벤트는 받지 않는다.
3. **배치 등록**: pr-batch에 배치를 등록할 때 `tenant_id: "dtest"`. pr-batch에서 tenant는 null에서 한 번만
   설정할 수 있다(이후 변경은 `409 TENANT_IMMUTABLE`). 기존 배치는 PATCH로 채운다.
4. **리포트 켜기**: 리포트 그래프를 구현한 뒤 Worker에 `AGENT_REPORT_ENABLED=true`. claim은 Stream에서
   이벤트를 받는 순간에만 일어나므로, 켜기 전에 끝난 배치는 리포트를 만들지 않는다. 롤아웃 중 구버전
   Pod가 섞여 있어도 claim형 이벤트는 IGNORED로 넘어가 막히지 않지만, 그 배치의 리포트는 생략될 수
   있다. 모든 Pod가 같은 설정이 된 뒤 켜는 것을 권장한다.

롤백: `ew_0007` downgrade는 claim형 Execution의 binding과 outbox·command·inbox 행을 삭제한다.

## 7. 파일별 변경 목록

| 파일 | 변경 내용 | 이유 |
|---|---|---|
| `src/app/worker_past/` (삭제) | 예전 Worker 코어 사본 삭제 | 어디서도 import하지 않는 죽은 코드 |
| `src/app/worker/contracts.py` | `Route(tenant, event_types, handler, claim=None)`, `RouteTable`(handler 키 `(tenant, event_type, claimed)`, `claims()`, `tenants`). `EventContext.tenant` 필수, `task_id` Optional | tenant·등록형/claim형 라우팅 |
| `src/app/worker/config.py` | `event_stream(tenant)`, `retention_days` | tenant Stream 이름, 보관 정리 |
| `src/app/worker/store.py` | `register(..., tenant)`(필수, 불변 검사), `claim(event, tenant)`(claim 이벤트부터 binding, 기존 binding 유지), `advance()`가 binding의 tenant·task_id로 Route 조회, `command()`가 tenant 반환, `purge()` | binding의 tenant, claim, 보관 정리 |
| `src/app/worker/ingress.py` | `Ingress(store, tenant, routes)`: envelope tenant 검증, claim이면 binding 생성. `EventRouter`가 RouteTable 사용 | tenant Stream 수신 |
| `src/app/worker/dispatcher.py` | handler를 `(tenant, event_type, task_id is None)`으로 조회. claim형인데 handler가 없으면 IGNORED | 롤아웃 중 무한 대기 방지 |
| `src/app/worker/runtime.py` | `ExecutorWorker(settings, routes)`. Route의 tenant마다 ingress consumer, Stream별 메트릭, 10분 주기 `purge` | tenant Stream 구독 |
| `src/app/worker/__init__.py` | `Route` export | 공개 API |
| `migrations/versions/0007_worker_binding_tenant.py` (신규) | `ew_bindings.tenant` 추가, `task_id` nullable. downgrade는 claim형 Execution 행 전부 삭제 | binding 변경 |
| `src/agent_config.py` | `AgentSettings.executor_tenant_id`(형식·예약어 검증), `require_executor_tenant()` | 필수 tenant |
| `src/main.py` | API lifespan 시작 시 `require_executor_tenant` | tenant 없으면 API 기동 실패 |
| `src/app/schemas/agents/orchestration_schema.py` | `ExecutorRequestBody.tenant_id`(Executor 형식 검증) | 제출에 tenant 포함 |
| `src/app/graphs/nodes/executor_request.py` | 제출 body에 `tenant_id=settings.executor_tenant_id` | 같음 |
| `src/app/agent_worker/graph_boundary.py` | `ExecutorBoundaryNodes(bindings, tenant)`, 등록 시 tenant 전달 | 등록형 binding에 tenant |
| `src/app/graphs/builders/build_analysis_workflow_graph.py` | boundary에 `settings.executor_tenant_id` 전달 | 같음 |
| `src/app/agent_worker/worker_hooks.py` | `build_handlers` → `build_routes(tenant, analysis, report)`: 분석 등록형 Route + 리포트 claim Route(같은 tenant) | Route 등록부 |
| `src/app/agent_worker/event_input.py` (신규) | `AgentEventInput`, `agent_event_input()`, `batch_completion()`(claim), `succeeded()`(실행 필터), `ExecutorApi`(REST 조회) | 리포트 입력·조회 |
| `src/app/agent_worker/claim_adapter.py` (신규) | `LangGraphClaimAdapter`: thread `{tenant}:{execution_id}`로 invoke, 재전달 시 skip/resume, 필터 | claim형 호출 |
| `src/app/graphs/builders/build_report_graph.py` (신규) | 리포트 그래프 골격(`ReportState`, `write_report` 미구현) | 리포트 개발자가 채울 자리 |
| `src/app/agent_worker/graph_provider.py` | `build_report_agent_graph()` | 그래프 빌드 지점 일원화 |
| `src/app/agent_worker/worker_main.py` | tenant 필수(`require_executor_tenant`), `HostSettings.report_enabled`. checkpointer pool과 그래프를 시작 시 1회 생성(이벤트마다 연결·빌드하던 코드와 주석 코드 제거) | Route 연결, 연결·지연 비용 제거 |
| `src/app/test/test_worker_core.py` (신규) | 실제 PostgreSQL·Redis로 코어 통합 검증 13건 + RouteTable 단위 3건 | 안전망 |
| `src/app/test/test_agent_worker_routes.py` (신규) | Route 구성, claim 필터, claim 어댑터(1회 실행·중단 후 resume·필터) 6건 | 신규 코드 검증 |
| `src/app/test/test_executor_submit_toggle.py` | 제출 tenant_id, tenant 검증·필수, 등록 tenant 3건 | 제출·설정 검증 |
| `src/app/test/test_executor_resume_persistence.py`, `test_executor_failure_recovery.py` | `EventContext`에 tenant 인자 추가 | 시그니처 변경 반영 |
| `.env.example`, `deploy/dtest-agent.yaml`, `cicd/basic/dev/deployment.yml` | `EXECUTOR_TENANT_ID=dtest`(cicd는 API·Worker 컨테이너 모두), `AGENT_REPORT_ENABLED=false`, `EW_RETENTION_DAYS=14` | 설정 |
| `docs/runtime_configuration.md` | 신규 환경변수, 구독 Stream 규칙 | 문서 |

## 8. 테스트

```bash
# 전체(통합 테스트는 아래 환경변수가 없으면 skip)
PYTHONSAFEPATH=1 PYTHONPATH=src:. .venv/bin/python -m unittest discover -s src/app/test -t src

# Worker 코어 통합 테스트: 버려도 되는 PostgreSQL DB와 Redis DB 번호
EW_TEST_DATABASE_URL=postgresql://USER:PASS@127.0.0.1:5432/<scratch_db> \
EW_TEST_REDIS_URL=redis://127.0.0.1:6379/15 \
PYTHONSAFEPATH=1 PYTHONPATH=src:. .venv/bin/python -m unittest app.test.test_worker_core
```

`test_worker_core`는 시작할 때 해당 DB에 alembic `upgrade head`를 실행하고, 테스트마다 고유한
namespace와 Stream을 쓴다. 주요 시나리오:
- 한 tenant에서 등록된 INTERACTIVE → 분석 handler, 미등록 BATCH 완료 → claim 후 리포트 handler,
  미등록 INTERACTIVE → 등록할 때까지 대기
- claim은 claim한 이벤트부터: 앞선 이벤트가 없어도 REST 이력 조회 없이 처리, 늦게 온 앞선 이벤트는 무시
- claim이 등록형 binding을 바꾸지 않음, claim Route가 없는 Pod에서는 IGNORED
- purge는 오래된 완료·미바인딩 행만 지우고 FAILED는 남김, 정리 후 재전달 무시
- tenant 불일치 DLQ, tenant Stream 2개 동시 라우팅, 순번·중복·REST 누락 복구·IGNORED·실패 한도

작업 시점 결과(로컬 PostgreSQL, Redis 6.0.8, 기준 bf6c58a): 343건 중 신규·변경 테스트는 모두 통과했다. 실패 2건,
오류 7건은 기준 커밋만 돌렸을 때(318건)와 같은 목록이다(`test_workflow_standard_compiler`, `test_workflow_compiler`,
`test_transform_data`, `test_mock_generation_concurrency`). 이 변경과는 무관하다. 그 밖에 확인한 것:
- ew_0007 upgrade/downgrade 왕복(claim형 행이 있는 DB 포함)
- Worker 기동: tenant 없음 → `EXECUTOR_TENANT_ID is required`로 실패, `dtest` → `ingress.dtest`만 구독하고 ready, SIGTERM으로 정상 종료
- ruff(F, E9) 통과

## 9. 알려진 제약과 결정 사항

- **리포트 에이전트 미구현**: `write_report`가 `NotImplementedError`다. 구현 전에 `AGENT_REPORT_ENABLED=true`면
  성공한 배치마다 5회 재시도 후 `FAILED`가 된다.
- **tenant `dtest`는 dtest-agent 전용**: 다른 서비스가 `dtest`로 배치를 등록하면 그 배치에도 리포트가 만들어진다.
  pr-batch는 호출 서비스를 구분하지 못해(같은 `iss`) 막을 수 없다.
- **의도적 변경 - checkpointer와 그래프 수명**: 이벤트마다 `AsyncPostgresSaver.from_conn_string`으로 연결을 열고
  그래프를 빌드하던 방식을, Worker 시작 시 `create_checkpointer`(연결 상태 검사가 있는 pool) 하나와 그래프
  1회 빌드로 바꿨다. 이벤트마다 연결을 맺는 비용과 지연이 사라진다. 원래 방식을 택했던 이유는 코드로 확인할
  수 없었다. 운영에서 연결 끊김이 보이면 pool 설정(`CHECKPOINT_POOL_*`)부터 본다. pool 최대 크기는
  `EW_DISPATCH_CONCURRENCY` 이상을 권장한다(기본 pool 4, dispatch 4).
- **보관 정리 기본 활성**: `EW_RETENTION_DAYS=14`. 처리 중이거나 실패한 행은 지우지 않는다(5절).
- **이전 binding 미처리**: 하위 호환을 지원하지 않으므로, 배포 전에 시작돼 공용 Stream으로 가던 실행의 이후
  이벤트와 tenant NULL binding은 처리하지 않는다.
- **dev 배포의 `EW_INSTANCE_ID` 고정값**: `cicd/basic/dev/deployment.yml`은 `dtest-agent-uid`로 고정돼 있어
  HPA로 Pod가 늘면 consumer 이름이 겹친다. 값을 지우거나(프로세스마다 UUID) Pod 이름으로 바꾸기를 권장한다(이번에 변경하지 않음).
- **유지한 구조**: Inbox → command Outbox → 내부 Redis Stream → Dispatcher 경로는 그대로다.
  binding은 정리하지 않는다(장기 정리 정책은 후속 과제).
