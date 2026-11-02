# 전 서비스 통합테스트 시나리오·진행방안 설계 리뷰 (2026-11-01)

기준 코드: executor `main` `170ad98` · pr-agentic-service `main` `f5281e1` · pr-batch `main` `06a3d9a` (사용자 기기 로컬 레포, 읽기만)
참고 문서: `integration-test-precheck-2026-11-01.md`, `final-go-no-go-2026-11-01.md`, `user-identity-employee-id-2026-11-01.md`, `error-format-unification-2026-11-01.md`, `executor-tenant-changes-for-agent-2026-11-01.md`, `logging-format-request-2026-11-01.md`, `executor-docs/*`, 각 레포 `docs/`

## 0. 결론

1. **통합테스트는 시작할 수 있는 상태입니다.** 코드로 확인해 보니 "남은 일"로 알려진 항목 중 세 가지는 이미 코드에 들어가 있습니다(사번 전환, 비활성 사번 차단, tenant 단위 정지). 남은 것은 **데이터 리셋**과 **환경 설정값(key 해시, 비밀번호, PVC, HPA max)** 입니다(3장).
2. **환경은 모두 dev로 확정했습니다(11/1 결정).** 세 서비스 모두 header 모드가 없어져 dev에서도 인증·tenant 매트릭스를 그대로 검증할 수 있습니다. 운영 이행 절차(빈 DB에서 Recreate 배포 → ops 등록 → 스모크)는 R5에서 dev를 한 번 더 리셋해 리허설합니다(4장).
3. **가장 큰 위험은 DB 연결 예산입니다.** 에이전트 문서의 예산표는 executor 최대 Pod를 1로 두었지만 executor에도 CPU HPA가 붙습니다. 에이전트 4 Pod(14×4=56) + pr-batch 2 Pod(6×2=12) + executor 2 Pod(16×2=32) = **100**으로, 사용 가능한 97을 넘습니다. 통합테스트의 스케일아웃 시나리오(G6)에서 실제 최대 연결을 재고, 결과에 따라 executor `DATABASE_MAX_OVERFLOW`를 5로 내리는 것을 판단합니다(9장 R1).
4. 시나리오는 8개 그룹 52개입니다. 진행은 R0(준비) → R1(스모크) → R2(기능 E2E) → R3(인증·오류·이벤트) → R4(스케일·장애) → R5(dev 재리셋 리허설) 순서이고, 각 라운드 합격 후 다음으로 넘어갑니다(6장).

## 1. 범위와 연동 구조

```
브라우저(SSO 쿠키) ─┐
외부 tenant(key) ───┼─▶ pr-agentic-service (min 2 / max 4, CPU HPA)
                    │      ├─ 대화 run → executor 실행 제출 (X-Api-Key, X-Tenant-Id, X-User-Id=사번)
                    │      ├─ /api/v1/batch/* → pr-batch 중계
                    │      ├─ Worker: executor.events.dtest-agent-service 구독(그룹 agentic:executor-events)
                    │      │          + 120초마다 누락 이벤트 보정(MissedEventRecovery)
                    │      └─ 배치 SUCCEEDED 이벤트 → executor 재조회 → 배치 리포트 생성
                    │
pr-batch (min 1 / max 2) ── 스케줄러 1초 루프 → Task(SUBMIT/SYNC/COLLECT/CANCEL) → executor
                            (이벤트를 구독하지 않고 5→60초 폴링)
executor (HPA, 시작 1) ── Worker(lease 60s, heartbeat 15s) → Jupyter Runtime Target(INTERACTIVE/BATCH)
                        └─ Outbox → Redis executor.events.<tenant>
Jupyter (Deployment별 replicas 1 고정, HPA 제외 전제) ── 공유 PV(RWX) /mnt/data
공용: PostgreSQL(max_connections 100) · Redis(DB 3, 비밀번호 공유)
```

호출자는 executor 기준 3개입니다: `pr-agentic-service`(relay, 기본 tenant `dtest-agent-service`, `["*"]`), `pr-batch`(relay, 기본 tenant 없음, `["*"]`), `ops`(admin).

## 2. 테스트 원칙

- **사용자 경로로 시작해 서비스 내부로 확인합니다.** 한 시나리오는 "에이전트 API 또는 화면에서 시작 → 각 서비스 DB·Redis·PV·로그에서 결과 확인"으로 닫습니다. 서비스 단독 기능은 각 레포 테스트(executor 844개 등)가 이미 덮으므로 다시 하지 않습니다.
- **증거는 남깁니다.** 시나리오마다 요청·응답, 관련 ID(run_id, execution_id, batch_id, batch_run_id, event_id), 로그 검색어(`request_id=…`, `execution_id=…`)를 결과표에 적습니다. 로그는 `key=value` 메시지 형식이므로 Kibana 검색어로 그대로 씁니다.
- **장애 주입은 R4에서만** 합니다. 그 전 라운드가 깨끗해야 장애 시나리오의 실패 원인을 가를 수 있습니다.
- **판정은 executor 상태를 진실로 봅니다.** 에이전트 run 상태와 pr-batch run 상태는 executor 실행 상태와 일치해야 합니다(불일치 자체가 결함).

## 3. 코드로 확인한 현재 상태

| 알려진 항목 | 코드 확인 결과 | 통합테스트에서 할 일 |
| --- | --- | --- |
| 에이전트 사번 전환 | **반영됨.** `api_service/users.py` `user_id_of()`가 새 사용자의 `users.id`를 소문자 사번으로 만들고, 이 값이 `X-User-Id`·`actor.id`로 나감(`ab788c9`). 기존 uuid 사용자는 리셋 전까지 uuid 유지 | **리셋 후에 시작**해야 모든 사용자가 사번이 됨. C1에서 확인 |
| 비활성 사번 차단 | **반영됨.** `deps.py` 179행: key 호출자가 보낸 `X-User-Id`가 우리 쪽 비활성 사용자면 403 | C3에서 확인 |
| tenant 단위 정지 | **반영됨.** pr-batch `POST /batches:disable-by-tenant`(`47d62cc`), 에이전트 `POST /admin/api-clients/tenants/{tenant}/revoke`가 호출(`f18a6e7`) | C5에서 확인 |
| header 모드 제거 | 세 서비스 모두 반영(executor `f03d9e8`, 에이전트 `68ac0b3`, pr-batch `4353cf7`). dev도 key 필수 | dev 설정에 실제 key·해시 필요(P2) |
| 오류 형식 통일 | executor 반영. 에이전트·pr-batch 반영 여부는 이 리뷰에서 다시 확인하지 않음 | D1 체크리스트로 확인 |
| dev/stg 결정 | 11/1 모두 dev로 확정 | 4장 |

## 4. 환경 결정 (11/1 확정: 모두 dev)

| 라운드 | 환경 | 내용 |
| --- | --- | --- |
| R1~R4 | dev | dev도 api_key 모드라 인증까지 실제 경로로 검증됨. 장애 주입(Pod 삭제, Redis 정지)도 dev에서 |
| R5 | dev (재리셋) | R4가 끝난 dev를 다시 리셋하고 이행 절차(빈 DB·PV, `Recreate`, ops 등록, 스모크)를 절차서대로 1회 실행 |

dev만 쓰므로 stg·prd 고유 항목은 이행 때 따로 확인해야 합니다: `APP_ENV=stg/prd` 이미지(build-arg와 Deployment env 일치), stg·prd `AUTH_CLIENTS` 해시, stg·prd에서 꺼지는 MCP·API 문서, 환경별 PVC·HPA 값. H3 스모크 묶음을 stg·prd 배포 직후 체크리스트로 그대로 씁니다.

전제: dev가 다른 환경과 DB·Redis·PV를 공유하지 않아야 합니다. 공유한다면 R4 장애 주입 시간을 따로 잡아야 합니다(인프라 확인 P8).

## 5. 사전 조건 (R0)

| # | 항목 | 담당 | 완료 확인 |
| --- | --- | --- | --- |
| P1 | 세 서비스 dev 이미지가 위 기준 커밋 이상 | 각 서비스 | 배포 이미지 태그·커밋 기록 |
| P2 | key 3종(에이전트, pr-batch, ops) 발급, executor `AUTH_CLIENTS` 해시 교체, 에이전트·pr-batch 설정에 key 입력 | executor·각 서비스 | executor `/workerz`를 ops key로 200, 다른 key로 403 |
| P3 | DB·Redis 비밀번호, `RUNTIME_CREDENTIAL_KEY`(Fernet) 실제 값 | 인프라 | executor `/readyz` 200, `/workerz` `redis_reachable: true` (Redis는 readiness를 막지 않으므로 꼭 확인) |
| P4 | 실제 PVC 이름을 executor·에이전트·Jupyter manifest에 통일, `/mnt/data/dtest/` 아래 `executor/`·에이전트 폴더 존재 | 인프라 | Jupyter Pod에서 `ls /mnt/data/dtest` |
| P5 | **데이터 리셋**: executor DB(빈 DB → `0014`), pr-batch 스키마, 에이전트 DB, PV `dtest/executor/`, Redis `executor.events*`·`agentic:*` | 각 서비스 | executor `alembic_version` = `0014`, 각 테이블 0건 |
| P6 | Jupyter Runtime Target 등록(ops key): INTERACTIVE 1개 이상, BATCH 1개 이상. R4용으로 INTERACTIVE 2개 권장 | executor | `GET /api/v1/runtime-targets` 모두 ACTIVE, probe 성공 |
| P7 | HPA 설정값 확인: 에이전트 2/4, pr-batch 1/2, executor ?/?, **Jupyter는 HPA 제외** | 인프라 | 플랫폼 화면 캡처. Jupyter에 HPA가 붙으면 커널 세션이 갈라지므로 시작 불가 |
| P8 | PostgreSQL `max_connections`, superuser 예약, 서비스별 role 연결 한도 | 인프라 | `SHOW max_connections`, `pg_roles.rolconnlimit` |
| P9 | 테스트 계정: 일반 사용자 2명(U1, U2), 관리자 1명(A1), 비활성화용 1명(U3). 외부 tenant key 1개(`ext-it`, 에이전트 관리자 화면에서 발급) | 진행 총괄 | 각 계정 SSO 로그인 성공, `users.id`가 소문자 사번 |
| P10 | 테스트 노트북·스크립트 세트: 정상(2 Step, Artifact 1개), 실패(2번째 Step 예외), 장시간(5분 sleep + 주기 출력), 대용량 출력, 메모리 초과 | 진행 총괄 | PV 입력 경로에 배치 |
| P11 | 관측 도구: Kibana 세 서비스 로그, `redis-cli`(XINFO/XLEN), DB 읽기 계정, `kubectl` 또는 플랫폼 Pod 삭제 권한 | 인프라 | 각 도구 접근 확인 |

## 6. 진행 순서와 역할

| 라운드 | 내용 | 시나리오 | 진입 조건 | 합격 조건 |
| --- | --- | --- | --- | --- |
| R0 준비 | 설정·리셋·등록 | P1~P11 | — | P 전 항목 완료 |
| R1 스모크 | 연결 확인 | A1, B1, E1 | R0 | 3개 모두 통과 |
| R2 기능 E2E | 대화형·배치 전체 흐름 | A2~A8, C1, F1~F6 | R1 | 전부 통과. 실패 시 수정 후 해당 그룹 재실행 |
| R3 경계 | 인증·tenant·오류·이벤트 | B2~B8, C2~C5, D1~D7, E2~E6 | R2 | 전부 통과. 오류 코드 불일치(D1)는 "알려진 차이"로 기록 후 진행 가능 |
| R4 스케일·장애 | HPA·Pod 손실·Redis·DB | G1~G9 | R3 | 데이터 손실·중복 실행 0건. 복구 시간이 기준 안 |
| R5 리허설(dev 재리셋) | 이행 절차 | H1~H3 + A1·A3·F1 | R4 | 절차서대로 1회에 성공 |

역할:

| 역할 | 하는 일 |
| --- | --- |
| 진행 총괄(chtype) | 라운드 진입·합격 판정, 결함 우선순위, 결과표 관리 |
| 에이전트 담당 | 화면·API 시작점, 에이전트 DB(run, executor_inbox, batch_report_jobs) 확인 |
| pr-batch 담당 | 배치 등록·예약·Task 상태 확인, 시각 조정 |
| executor 담당 | 실행·이벤트·Runtime 상태 확인, ops 작업(등록, drain) |
| 인프라 | HPA·Pod 삭제·Redis/DB 장애 주입, 연결 수 측정 |

결함 처리: 발견 즉시 결과표에 시나리오 ID와 증거를 남기고, 차단(진행 불가)·주요(우회 가능)·경미로 나눕니다. 차단은 수정 후 그 라운드를 처음부터, 나머지는 해당 시나리오만 재실행합니다.

## 7. 시나리오

표기: **절차**는 요약, **합격**은 확인할 사실입니다. 에이전트 경로는 `/api/v1` 아래입니다.

### A. 정상 흐름 E2E

| ID | 시나리오 | 절차 | 합격 |
| --- | --- | --- | --- |
| A1 | 대화형 분석 SINGLE | U1 로그인 → 프로젝트·세션 생성 → 분석 질문으로 run 시작(`POST /sessions/{id}/runs`) → SSE(`/sessions/{id}/events`) 관찰 | run이 executor 대기(`waiting_*`) 후 재개되어 최종 답변. executor 실행 `SUCCEEDED`, `context.tenant_id=dtest-agent-service`, `context.user_id=U1 사번`. 이벤트 6종 순서(started → operation_started → step_started → step_completed → operation_completed → completed). 결과 노트북·Artifact를 화면에서 열람 |
| A2 | MULTI 연속 Operation | 같은 세션에서 후속 분석 질문 2회 | 같은 execution에 Operation 2개, 상태 `WAITING_FOR_OPERATION` ↔ `RUNNING`, finalize 후 `execution.completed` 1회 |
| A3 | 노트북 → 배치 등록 | A1 결과로 `POST /batch/scripts/from-notebook` → `POST /batch/batches`(ONCE, 2분 뒤) | pr-batch에 (tenant=`dtest-agent-service`, owner=U1 사번)로 저장. 예약 시각에 Run 생성 → executor 제출(`trigger.type=BATCH`, BATCH 풀) → pr-batch Run `COMPLETED` |
| A4 | 배치 리포트 | A3 실행 완료 후 | 에이전트가 `execution.completed` 수신 → executor 재조회 → `batch_report_jobs` 1건(멱등 키 `batch-report:<execution_id>`) → `/batch-reports`에서 U1만 조회 가능, SSE로 진행 표시 |
| A5 | 수동 실행 | `POST /batch/batches/{id}/runs` | 즉시 Run 생성, 나머지는 A3과 같음 |
| A6 | 반복 일정 | INTERVAL 60초 배치를 5분 유지 후 disable | Run 5건 내외, 각 1회 제출(멱등 키 `batch-run:<run_id>:submit` 중복 없음), disable 후 신규 Run 없음 |
| A7 | 대화 run 취소 | 장시간 노트북 실행 중 `POST /runs/{id}/cancel` | executor 실행 `CANCELLED`, 커널 정리, 에이전트 run 종료. 같은 세션에서 다음 run 정상 |
| A8 | 배치 Run 취소 | 장시간 배치 실행 중 `POST /batch/batches/{id}/runs/{run_id}/cancel` | pr-batch CANCEL Task → executor `CANCELLED` → pr-batch Run `CANCELLED`, 리포트 미생성 |

### B. 인증과 tenant

| ID | 시나리오 | 절차 | 합격 |
| --- | --- | --- | --- |
| B1 | key 없이 호출 | 세 서비스 API에 key 없이 | 모두 `401 UNAUTHENTICATED`. `/healthz`·`/readyz`는 200 |
| B2 | 잘못된 key, 빈 key | `X-Api-Key: wrong`, `X-Api-Key: ` | 401 |
| B3 | 운영 API 권한 | executor `/runtime-targets`, `/maintenance/drain`, `/workerz`를 에이전트·pr-batch key로 | 403. ops key로는 200 |
| B4 | pr-batch key tenant 누락 | pr-batch key로 executor에 `X-Tenant-Id` 없이 | `400 INVALID_IDENTITY` |
| B5 | tenant 형식·예약어 | `X-Tenant-Id: dlq`, `Bad_Tenant`, `*` | 400 (`"*"` key여도) |
| B6 | tenant 격리 | 외부 tenant `ext-it` key로 실행 → 에이전트 기본 tenant로 그 execution 조회 | 404. `ext-it` 지정 조회는 200. 이벤트는 `executor.events.ext-it`에만 있음 |
| B7 | 사용자 격리 | U1 실행·배치·리포트를 U2로 조회·취소 | 404 |
| B8 | 본문 위조 | 실행 제출 본문에 `context`·`tenant_id` 포함, `actor.id`≠`X-User-Id` | 422 (`actor_user_mismatch` 포함) |

### C. 사용자 식별과 수명주기

| ID | 시나리오 | 절차 | 합격 |
| --- | --- | --- | --- |
| C1 | 사번 일관성 | A1·A3 후 세 DB 조회 | 에이전트 `users.id`, executor `executions.user_id`, pr-batch `owner_id`, executor 작업공간 `tenants/dtest-agent-service/users/<사번>/`가 모두 같은 소문자 사번 |
| C2 | 대문자 사번 | 외부 key로 `X-User-Id: E12345` | 소문자로 저장, 같은 사람으로 조회 |
| C3 | 비활성 사번 차단 | A1이 U3 비활성화(`PATCH /admin/users/{id}`) → 외부 key로 `X-User-Id=U3 사번` 배치 등록·enable | 403 |
| C4 | 비활성화 → 배치 정지 | U3가 우리 tenant와 `ext-it`에 배치 보유 상태에서 비활성화 | 두 tenant의 U3 배치 모두 disabled(`USER_DEACTIVATED`), U1 배치는 유지. pr-batch 장애 중이면 `batches_disable_pending`이 남고 복구 후 재시도로 완료 |
| C5 | tenant 폐기 | `POST /admin/api-clients/tenants/ext-it/revoke` | `ext-it` key 401, pr-batch의 `ext-it` 배치 모두 disabled(`TENANT_REVOKED`), 이미 실행 중인 executor 실행은 끝까지 진행(정책) |

### D. 오류 처리

| ID | 시나리오 | 절차 | 합격 |
| --- | --- | --- | --- |
| D1 | 오류 형식 | 세 서비스에 없는 경로, 잘못된 메서드, 형식 오류 본문 | 404 `NOT_FOUND`, 405 `METHOD_NOT_ALLOWED`, 422 `REQUEST_VALIDATION_ERROR`, `details` 항상 존재(`error-format-unification` 체크리스트) |
| D2 | Step 실패 → Retry | 실패 노트북 실행 → 화면/API에서 retry | 1차 `FAILED`(`retry.allowed=true`), retry 후 실패 Step부터 재실행, `execution.started` 재발행 없음 |
| D3 | 배치 실행 실패 | 실패 스크립트로 배치 | pr-batch Run `FAILED`, `execution_status`·`failure_type` 보존, 리포트 미생성 |
| D4 | Runtime 없음 | BATCH Target을 disable한 상태로 배치 실행 | 실행이 `QUEUED`로 대기(다른 풀로 넘어가지 않음), Target activate 후 진행 |
| D5 | 노트북 내부 커널 사망 | 메모리 초과 노트북 | `RUNTIME_SESSION_LOST` 분류, 에이전트가 사용자에게 실패 안내, 다음 run 정상 |
| D6 | 입력 경로 탈출 | 실행 입력에 `../` 경로 | 4xx로 거부(코드 확인 필요), PV 밖 파일 접근 없음 |
| D7 | 에이전트의 하위 오류 표시 | D1~D3 상황을 화면에서 | pr-batch·executor 오류가 사용자에게 이해 가능한 문구로 표시, 내부 SQL·스택 없음 |

### E. 이벤트와 스트림

| ID | 시나리오 | 절차 | 합격 |
| --- | --- | --- | --- |
| E1 | 스트림 연결 | A1 후 `XINFO GROUPS executor.events.dtest-agent-service` | 그룹 `agentic:executor-events` 존재, pending 0 |
| E2 | 중복 전달 | 같은 event_id가 두 번 처리되도록 pending 상태에서 에이전트 Pod 재시작 | `executor_inbox` 1건, run 재개 1회 |
| E3 | 위조 이벤트 | `XADD`로 존재하는 execution_id의 가짜 `execution.completed` | 에이전트가 executor 타임라인으로 확인 후 버림(로그 확인), run 상태 변화 없음, 리포트 미생성 |
| E4 | 이벤트 유실 보정 | 실행 중 에이전트 consumer 그룹의 해당 메시지를 ACK 처리해 유실 모사(또는 G4 Redis 정지와 묶음) | 최대 약 120초(`EXECUTOR_EVENT_RECOVERY_SECONDS`) 안에 MissedEventRecovery가 run 재개 |
| E5 | 이벤트가 먼저 도착 | 매우 짧은 노트북으로 제출 직후 완료 | run이 대기 들어가기 전 이벤트가 와도 inbox로 재개(멈춤 없음) |
| E6 | 외부 tenant 이벤트 | `ext-it` 실행 완료 | `executor.events.ext-it`에 발행. 에이전트는 구독하지 않으므로 리포트·run 변화 없음(의도된 동작인지 확인 필요, 9장 R4) |

### F. 배치

| ID | 시나리오 | 절차 | 합격 |
| --- | --- | --- | --- |
| F1 | 일정 3종 | ONCE, INTERVAL(60초), CRON(매분) 각 1개 | 예정 시각 ±2초 안에 Run 생성, 중복 없음 |
| F2 | 겹침 | 장시간(5분) 스크립트를 INTERVAL 60초로 | 실행 중 도래분은 SKIPPED·skipped-windows에 기록 |
| F3 | 동시 실행 한도 | 한도 초과 수동 실행 | 초과분 `WAITING` → 자리 나면 `QUEUED` |
| F4 | 중지·재개 | 실행 중 disable → enable | 실행 중인 것은 끝까지, 중지 기간 일정은 소급 안 함 |
| F5 | 배치 수정 | 예약 대기 중 스크립트 새 버전·시각 변경 | 다음 Run이 새 버전·새 시각 사용 |
| F6 | 결과 수집 | 성공 Run의 결과·Artifact를 에이전트 화면에서 | pr-batch `COLLECTING → COMPLETED`, 결과 경로가 `/mnt/data/dtest/executor/…` |

### G. HPA·스케일·장애 조치 (R4, 인프라 동반)

| ID | 시나리오 | 절차 | 합격 |
| --- | --- | --- | --- |
| G1 | executor 스케일아웃 | executor replicas 1→2(또는 부하로 HPA 유도), 실행 20건 동시 제출 | 두 Pod가 나눠 claim(`/workerz` 확인), 같은 실행 중복 실행 0건, 모두 종료 |
| G2 | executor 스케일인 | 장시간 실행 중 Pod 2→1 | 종료 Pod가 drain → 실행 중 Step이 남은 Pod로 인계(`step_handover_started/completed`) 또는 `WORKER_SHUTDOWN` 정책대로 종료. QUEUED 실행은 남은 Pod가 1회만 claim |
| G3 | executor Pod 강제 삭제 | 장시간 실행 중 `kubectl delete pod --grace-period=0` | lease 만료(60초) 후 다른 Pod가 이어받음. 결과 1벌, 이벤트 순서 유지. 단일 Pod였다면 새 Pod 기동 후 복구 |
| G4 | Redis 정지 | 실행 진행 중 Redis 2분 정지 | executor 실행은 계속(Outbox에 적재), 복구 후 이벤트 순서대로 발행. 에이전트 run은 복구 후 또는 보정 주기 안에 재개. 세 서비스 API는 Redis 의존 기능 외 정상 |
| G5 | DB 순간 단절 | DB 연결 끊김 30초(인프라가 가능할 때) | 세 서비스 `503 DATABASE_UNAVAILABLE`(executor·pr-batch), 복구 후 자동 정상. 진행 중 실행 상태 손실 없음 |
| G6 | **DB 연결 최대치 측정** | 에이전트 4 Pod, pr-batch 2 Pod, executor 2 Pod로 고정 후 동시 부하(에이전트 `scripts/loadtest.py` + 배치 다수 수동 실행) | `pg_stat_activity` 최대 연결 수 기록. **97 이하**이고 `QueuePool limit`·`too many clients` 로그 0건. 넘으면 9장 R1 조치 후 재측정 |
| G7 | 에이전트 Pod 손실 | executor 대기 중인 run이 있는 에이전트 Pod 삭제 | 다른 Pod가 lease 회수 후 이벤트로 재개, SSE 재연결 시 이벤트 재생 |
| G8 | pr-batch Pod 손실 | SUBMIT Task 처리 중 Pod 삭제 | lease(90초) 후 다른 Pod가 재시도, 같은 멱등 키로 executor 실행 1건만 존재 |
| G9 | Jupyter 재시작 | 실행 중 Jupyter Pod 삭제 | `RUNTIME_SESSION_LOST`로 종료(무한 RUNNING 없음), Target 재기동 후 probe 정상, 다음 실행 성공. 배치는 pr-batch Run `FAILED` |

### H. 데이터 리셋과 릴리스 리허설 (R5, dev 재리셋)

| ID | 시나리오 | 절차 | 합격 |
| --- | --- | --- | --- |
| H1 | 리셋 절차 | P5 절차서를 dev에서 다시 그대로 실행 | 세 DB 빈 상태, PV `dtest/executor/` 비어 있음, Redis 키 없음. 걸린 시간 기록 |
| H2 | 첫 배포 | executor `Recreate`, `APP_ENV=dev`(이미지 build-arg와 Deployment env 둘 다), 에이전트·pr-batch 배포 | executor 자동 migration `0014`, 세 서비스 Ready, ops key로 Target 등록 |
| H3 | 이행 직후 스모크 | A1, A3(ONCE 2분 뒤), B1, F1 일부 | 전부 통과. 이 묶음을 prd 이행 직후 체크리스트로 재사용 |

## 8. 전체 합격 기준

1. R1~R4 시나리오 전부 통과, 차단·주요 결함 0건.
2. 데이터 정합성: 시나리오 전체에서 **중복 실행 0건, 유실 실행 0건**, 세 서비스 상태 불일치 0건(종료 후 일괄 대조 쿼리로 확인).
3. 이벤트: 모든 실행의 `execution.completed`가 tenant 스트림에 있고, 에이전트 inbox pending 0.
4. 복구 시간: executor Pod 손실 후 이어받기 ≤ 90초(lease 60 + 여유), 이벤트 유실 보정 ≤ 150초, Redis 복구 후 Outbox 적체 0까지 ≤ 60초.
5. DB 연결 최대치 ≤ 97(G6).
6. 로그: 각 시나리오 ID로 세 서비스 로그를 Kibana에서 `request_id`·`execution_id`로 따라갈 수 있고, 비밀값(key, 비밀번호, SQL 파라미터) 노출 0건.
7. R5 리허설이 절차서 수정 없이 1회에 성공.

## 9. 리뷰에서 발견한 위험과 보완

| # | 내용 | 영향 | 권장 |
| --- | --- | --- | --- |
| R1 | **DB 연결 예산 초과 가능.** 에이전트 `docs/load-design.md` 표는 executor 최대 Pod를 1로 계산(최대 84)했지만 executor에도 HPA가 붙음. 에이전트 4 + pr-batch 2 + executor 2 = 100 > 97 | 피크 시 `too many clients`로 세 서비스 동시 장애 | G6에서 실측. 넘으면 executor `DATABASE_MAX_OVERFLOW: 5`(Pod당 11 → executor 2 Pod 22, 합계 90). executor HPA max를 2로 고정 요청. 에이전트 문서 표 수정 |
| R2 | **Jupyter HPA 제외가 플랫폼에서 보장되는지 미확인.** "CPU HPA 자동 적용"이 모든 Deployment에 붙으면 Jupyter가 2개로 늘어 커널 세션이 갈림 | 실행 무작위 실패 | P7에서 확인. 제외가 안 되면 min=max=1 HPA로 고정 |
| R3 | 리셋 전 생성된 uuid 사용자 | 리셋 없이 시작하면 C1 실패, 비활성화가 tenant를 넘지 못함 | P5 리셋을 생략하지 않음 |
| R4 | 외부 tenant 배치는 리포트가 생기지 않음(에이전트가 자기 tenant 스트림만 구독) | 외부 tenant가 리포트를 기대하면 기능 공백 | 의도 확인. 의도라면 외부 tenant 안내 문서에 명시 |
| R5 | pr-batch는 폴링(최대 60초 간격)으로 종료를 봄 | 배치 Run 완료 표시가 executor보다 최대 60초 늦음. 리포트(이벤트 기반)가 pr-batch `COMPLETED`보다 먼저 생길 수 있음 | 합격 기준에서 시간 차를 허용. 화면이 둘의 순서를 가정하지 않는지 A4에서 확인 |
| R6 | 사용자 비활성화·tenant 폐기는 실행 중인 executor 실행을 멈추지 않음 | 정책상 허용 | C4·C5에서 "끝까지 실행"을 합격으로 명시(정책 확인용) |
| R7 | 에이전트 레포에 추적 안 된 `인증가이드.md`(pr-auth 토큰 방식, 10/29)가 있음 | 현재 api_key 방식과 다른 설계라 테스트 참여자가 혼동할 수 있음 | 폐기 문서로 표시하거나 레포 밖으로 이동 |

## 10. 결정 필요

1. ~~환경~~ → 11/1 모두 dev로 확정. dev가 다른 환경과 DB·Redis·PV를 공유하는지만 인프라 확인(P8).
2. **executor HPA max와 DB 예산(R1)**: G6 실측 전에 `DATABASE_MAX_OVERFLOW: 5`를 미리 넣을지, 실측 후 결정할지. 권장은 실측 후 결정(설정만 바꾸면 되므로).
3. **외부 tenant 배치 리포트(R4)**: 지원 안 함으로 확정할지.

## 부록. 시나리오 결과표 양식

| ID | 라운드 | 일시 | 수행자 | 결과(통과/실패/보류) | 주요 ID(run/execution/batch/event) | 증거(로그 검색어, 캡처) | 결함 번호 |
| --- | --- | --- | --- | --- | --- | --- | --- |
