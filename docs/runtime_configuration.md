# Executor 실행·결과 설정 가이드

## 1. 목적

이 문서는 Agent가 Executor에 Notebook 셀과 리포트를 전달하는 방식, 실행 결과를
읽는 방식, Redis와 PostgreSQL 연결 설정을 설명한다. 환경변수를 변경한 뒤에는
LangGraph API와 Agent Worker를 모두 재시작해야 한다.

## 2. 권장 기본 설정

로컬 개발에서는 기존 API 결과 조회 방식을 기본으로 사용한다.

```env
EXECUTOR_SOURCE_TYPE=INLINE
EXECUTOR_REPORT_SOURCE_TYPE=INLINE
EXECUTOR_RESULT_READ_MODE=API
EXECUTOR_SHARED_INPUT_ROOT=/workspace/pv
EXECUTOR_SHARED_RESULT_ROOT=/workspace/pv
```

Agent와 Executor가 동일한 PV를 마운트한 배포 환경에서는 다음 조합을 사용할 수
있다.

```env
EXECUTOR_SOURCE_TYPE=PATH
EXECUTOR_REPORT_SOURCE_TYPE=PATH
EXECUTOR_RESULT_READ_MODE=MANIFEST
EXECUTOR_SHARED_INPUT_ROOT=/workspace/pv
EXECUTOR_SHARED_RESULT_ROOT=/workspace/pv
```

## 3. Notebook 셀 전달 방식

`EXECUTOR_SOURCE_TYPE`은 Executor에서 실행할 Python 셀 코드의 전달 방식을 정한다.

| 값 | 동작 | 요구사항 |
|---|---|---|
| `INLINE` | 실행 요청 JSON에 Python 코드를 직접 넣는다. | 공유 PV가 없어도 된다. |
| `PATH` | 공유 PV에 `.py` 파일을 저장하고 상대경로와 SHA-256을 보낸다. | Agent와 Executor가 같은 PV를 봐야 한다. |

`PATH`에서 파일은 `EXECUTOR_SHARED_INPUT_ROOT` 아래에 적재된다. API 요청에는 절대
경로가 아니라 공유 루트 기준 상대경로가 들어간다.

## 4. 최종 리포트 전달 방식

`EXECUTOR_REPORT_SOURCE_TYPE`은 생성된 Markdown 리포트를 artifact API에 전달하는
방식을 정한다.

| 값 | 동작 |
|---|---|
| `INLINE` | Markdown 본문을 artifact POST body에 직접 넣는다. |
| `PATH` | 공유 PV에 `final-report.md`를 적재하고 상대경로와 SHA-256을 보낸다. |

두 방식 모두 최종적으로 다음 API를 호출한다.

```http
POST /api/v1/executions/{execution_id}/artifacts
```

요청 artifact type은 `REPORT`, 이름은 `final-report.md`다.
`EXECUTOR_REPORT_APPEND_TO_NOTEBOOK=true`이면 Executor가 리포트를 실행 Notebook에도
추가한다. 최종 artifact 저장 위치는 Executor가 관리하며 일반적으로 해당 실행의
`artifacts/reports/` 아래다.

## 5. 실행 결과 읽기 방식

`EXECUTOR_RESULT_READ_MODE`은 Operation 완료 후 함수 실행 결과를 수집하는 방식을
정한다. 허용값은 `API`, `MANIFEST`다.

### 5.1 API

기존 구현이며 기본값이다.

```text
GET /api/v1/executions/{execution_id}/result
GET /api/v1/executions/{execution_id}/notebook?view=FULL
→ Step 상태와 Notebook output 결합
```

```env
EXECUTOR_RESULT_READ_MODE=API
```

### 5.2 MANIFEST

`execution.operation_completed` 이벤트의
`payload.step_results[].result_ref.relative_path`를 이용한다.

```text
operation_completed 이벤트
→ result_ref가 가리키는 manifest.json
→ manifest의 outputs[].representations[]
→ 실제 텍스트·JSON output 파일
→ 기존 executor_result_history 형식으로 정규화
```

```env
EXECUTOR_RESULT_READ_MODE=MANIFEST
EXECUTOR_SHARED_RESULT_ROOT=/workspace/pv
```

`execution.completed`에는 `operation`과 Step별 `result_ref`가 없다.

- SINGLE: 종료 이벤트를 받으면 Result API에서 `operations[].steps[].result.result_ref`를 조회하고, 실제 출력은 공유 PV에서 읽는다. MANIFEST 모드에서도 이 인덱스 조회는 필요하며 Notebook GET은 하지 않는다.
- MULTI: 보통 `execution.operation_completed`에서 결과를 수집한다. 전체 실패·취소로 종료됐으면 추가 셀이나 취소 요청 없이 실패를 안내하고 종료한다. 작업 완료 이벤트를 기다리는 중 전체 종료 이벤트가 먼저 와도 처리한다.
- 실패·취소 Step의 `result_ref`가 null이면 결과 파일 없이 오류 요약을 사용한다. 성공 Step의 참조 누락은 결과 수집 오류로 취급한다.
- Result API·Manifest 읽기에 실패해도 알려진 실행 상태와 결과 수집 오류를 메시지로 남긴다. 결과가 불완전하면 분석 리포트를 생성하지 않는다.

Manifest Reader는 다음을 검증한다.

- 공유 루트 이탈, 절대경로 및 `..` 차단
- manifest SHA-256 및 제공된 경우 크기, output 파일의 크기 및 SHA-256
- Manifest schema version `1.0`
- execution ID, Operation ID, Step ID, sequence, attempt ID
- 제공된 경우 fencing token
- UTF-8 텍스트와 JSON 본문 로드
- 이미지 등 바이너리는 본문 대신 파일 참조 정보 유지

Manifest 원문이 크더라도 Report LLM에는 압축된 결과를 전달한다. 이 압축은 API와
MANIFEST 모드 모두에 적용된다.

Manifest 및 출력 파일의 상대경로는 현재 Executor 계약에 따라 모두 공유 PV 루트에 결합한다.

이 처리는 이벤트 수신 이후의 오류를 다룬다. 이벤트 미전달 시 자동 상태 재조회·대기 제한은 포함하지 않는다.
이미 `ew_commands.state=FAILED`로 종료된 명령은 코드 배포만으로 자동 재처리되지 않는다.
새 실행으로 검증하거나 기존 Worker의 재처리 절차를 별도로 사용해야 한다. 노트북 실행 자체를 재시도하는 것과 이벤트 재처리는 다르다.

## 6. 공유 PV 설정

| 환경변수 | 역할 |
|---|---|
| `EXECUTOR_SHARED_INPUT_ROOT` | Agent가 셀 코드와 PATH 방식 리포트 원본을 적재하는 루트 |
| `EXECUTOR_SHARED_RESULT_ROOT` | Agent가 Executor의 Step manifest와 output을 읽는 루트 |

두 값은 보통 모두 `/workspace/pv`지만 역할이 다르므로 별도 설정으로 관리한다.
컨테이너 내부의 Agent와 Executor에서 동일한 파일이 동일한 의미의 경로로 보여야
한다.

## 7. 기타 Executor 설정

| 환경변수 | 설명 |
|---|---|
| `EXECUTOR_BASE_URL` | Executor API 기본 URL |
| `EXECUTOR_*_PATH` | 실행 생성, Operation, 결과, Notebook, finalize, cancel, artifact endpoint |
| `EXECUTOR_RUNTIME_PROFILE` | Executor Runtime profile |
| `EXECUTOR_TENANT_ID` | 필수. 이 서비스의 Executor tenant(예: `dtest`). 분석 에이전트가 이 `tenant_id`로 제출하고 Worker는 `{EW_EXECUTOR_EVENT_STREAM}.{tenant}`만 받는다. pr-batch 배치 등록에도 같은 값. API와 Worker에 같은 값. 형식이 틀리거나 `dlq`·`work`면 시작 시 실패 |
| `EXECUTOR_TLS_VERIFY` | TLS 인증서 검증 여부. 운영에서는 `true` 권장 |
| `EXECUTOR_TIMEOUT_SECONDS` | HTTP 요청 timeout |
| `EXECUTOR_OPERATION_TIMEOUT_SECONDS` | Operation 실행 timeout |
| `EXECUTOR_OPERATION_WAIT_TIMEOUT_SECONDS` | 다음 Operation 대기 timeout |
| `EXECUTOR_SUBMIT_ENABLED` | 실제 Executor 요청 전송 여부 |
| `DATA_MOCK` | `true`이면 데이터 선택은 유지하되 wide mock parquet을 직접 로드 |

## 8. Redis 설정

Redis는 Executor 원본 이벤트와 Worker 내부 command 전달에 사용된다.

| 환경변수 | 설명 |
|---|---|
| `EW_REDIS_URL` | Worker가 연결할 Redis URL |
| `EW_EXECUTOR_EVENT_STREAM` | Executor 이벤트 Stream 이름(`REDIS_EVENT_STREAM`). 기본 `executor.events`. 실제 구독은 `{값}.{EXECUTOR_TENANT_ID}` |
| `EW_EVENT_GROUP_NAME` | 원본 이벤트 ingress consumer group |
| `EW_COMMAND_STREAM_NAME` | Worker가 내부적으로 생성하는 command Stream |
| `EW_COMMAND_GROUP_NAME` | LangGraph resume dispatch consumer group |
| `EW_NAMESPACE` | Redis key와 Worker DB 행의 서비스 구분자 |
| `EW_INSTANCE_ID` | Worker replica 식별자. replica마다 달라야 한다(미지정 시 프로세스마다 UUID) |
| `EW_RETENTION_DAYS` | 처리가 끝났거나 아무도 받지 않는 Inbox/command 행 보관 일수. 기본 14, 0이면 정리 안 함 |
| `AGENT_REPORT_ENABLED` | `true`면 `EXECUTOR_TENANT_ID` tenant의 BATCH 실행이 성공했을 때 리포트 에이전트를 실행한다. 기본 `false` |

Worker는 `{EW_EXECUTOR_EVENT_STREAM}.{EXECUTOR_TENANT_ID}` Stream만 구독한다. 자세한 구조는
[tenant 이벤트 라우팅 인수인계](./handover/tenant-event-routing.md)를 참고한다.

처리 흐름은 다음과 같다.

```text
Executor Outbox → executor.events.{tenant}
→ Worker ingress(Stream별) → PostgreSQL Inbox
→ Worker router/outbox → 내부 command Stream
→ Worker dispatch → LangGraph Command(resume=...)
```

Redis Streams는 at-least-once 전달이므로 `event_id`와 `command_id` 기반 멱등 처리가
필수다. Redis는 상태 원본이 아니며, Worker의 영속 처리 상태는 PostgreSQL에 있다.

## 9. PostgreSQL 설정 요약

| 환경변수 | 사용 주체 | 역할 |
|---|---|---|
| `EW_DATABASE_URL` | Agent Worker | event Inbox/Outbox, command, execution binding 및 기본 Workflow 저장소 |
| `WORKFLOW_DATABASE_URL` | Agent | 지정 시 Workflow catalog 저장소를 `EW_DATABASE_URL` 대신 사용 |
| `AGENT_CHECKPOINT_DATABASE_URL` | LangGraph API와 Worker | 동일 thread를 재개하기 위한 LangGraph checkpoint DB |
| `CHECKPOINT_DB_URI` | 로컬 CLI `--postgres` 경로 | CLI용 PostgreSQL checkpointer |

LangGraph API와 Worker의 `AGENT_CHECKPOINT_DATABASE_URL`은 반드시 같아야 한다. 서로
다르면 Worker가 Redis 완료 이벤트를 받아도 API가 만든 중단 checkpoint를 찾거나
재개할 수 없다.

DB 생성과 마이그레이션은
[database_migrations.md](./database_migrations.md)를 참고한다.

