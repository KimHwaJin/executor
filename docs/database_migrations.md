# Agent·Worker PostgreSQL 및 Alembic 가이드

## 1. DB 역할 구분

이 프로젝트에는 성격이 다른 두 종류의 PostgreSQL 데이터가 있다.

| 구분 | 환경변수 | 관리 방식 |
|---|---|---|
| Worker 및 Workflow 업무 테이블 | `EW_DATABASE_URL` | 저장소의 Alembic migration |
| LangGraph checkpoint 테이블 | `AGENT_CHECKPOINT_DATABASE_URL` | `AsyncPostgresSaver.setup()` |

로컬 CLI에서 `python cli.py --postgres`를 사용할 때는
`CHECKPOINT_DB_URI`를 사용한다. LangGraph dev/API와 Worker 간 자동 resume에는
`AGENT_CHECKPOINT_DATABASE_URL`이 기준이다.

## 2. Alembic 구성

주요 파일:

```text
alembic.ini
migrations/env.py
migrations/versions/0001_worker_tables.py
migrations/versions/0002_workflow_catalog.py
```

`alembic.ini`는 migration 디렉터리와 로깅만 설정한다. DB URL은 파일에 저장하지
않고 `migrations/env.py`가 `EW_DATABASE_URL`에서 읽는다.

Alembic version table 이름은 기본 `alembic_version`이 아니라 다음 값이다.

```text
ew_alembic_version
```

Migration 시작 시 PostgreSQL advisory transaction lock을 획득하므로 여러 replica가
동시에 migration을 시작해도 동일 DDL을 중복 적용하지 않는다.

## 3. Revision 구성

### `ew_0001`: Worker 이벤트 전달 테이블

| 테이블 | 역할 |
|---|---|
| `ew_bindings` | Executor execution과 LangGraph session/task 연결 |
| `ew_inbox` | Redis에서 받은 Executor 원본 이벤트 영속화 |
| `ew_commands` | LangGraph resume 대상 command와 처리 상태 |
| `ew_outbox` | 내부 Redis command Stream 발행 상태 |
| `ew_audit` | 실패 command의 retry/skip 운영 이력 |

`namespace`, `event_id`, `execution_id`, `sequence`를 이용해 중복과 순서를 관리한다.
Command 상태는 `READY`, `RUNNING`, `DONE`, `FAILED`, `IGNORED`다.

### `ew_0002`: Workflow 저장 및 실행 이력

| 객체 | 역할 |
|---|---|
| `workflow_catalog` | 재사용 가능한 승인 Workflow와 검증 상태 |
| `workflow_executions` | 원본/최종 Workflow 및 실행 결과 |
| `workflow_adaptive_history` | Adaptive 조건부 Tool 결정 이력 |
| `workflow_adaptive_history_view` | 실행과 Adaptive 변경 이력 결합 조회 |

기본 구현은 이 테이블도 `EW_DATABASE_URL`에 둔다.
`WORKFLOW_DATABASE_URL`을 별도 DB로 지정하려면 해당 DB에도 필요한 Workflow
revision을 적용하는 별도 배포 절차가 필요하다. 현재 Alembic `env.py`는
`EW_DATABASE_URL`만 직접 읽으므로 기본 구성에서는 두 URL을 분리하지 않는 것을
권장한다.

## 4. Migration 실행

저장소 루트에서 `.env`를 환경에 로드한 후 실행한다.

```bash
set -a
source .env
set +a
alembic current
alembic upgrade head
```

가상환경의 실행 파일을 명시하려면:

```bash
../.venv311/bin/alembic upgrade head
```

운영 배포 권장 순서:

1. PostgreSQL 연결 및 백업 정책 확인
2. `EW_DATABASE_URL`이 대상 DB인지 확인
3. `alembic current`와 `alembic history` 확인
4. 배포 Job에서 `alembic upgrade head` 한 번 실행
5. LangGraph API와 Worker 기동
6. Worker `/health/ready` 확인

Application replica마다 Alembic을 실행하기보다 별도의 migration Job 또는 init
단계에서 한 번 실행하는 편이 좋다.

## 5. LangGraph checkpoint 초기화

LangGraph checkpoint 테이블은 위 Alembic revision에 포함되지 않는다.

```env
AGENT_CHECKPOINT_DATABASE_URL=postgresql://...
CHECKPOINT_SETUP_ON_START=true
```

`langgraph dev`는 `langgraph.json`에 등록된 custom checkpointer factory를 통해
`AsyncPostgresSaver.setup()`을 실행한다. Agent Worker도 같은 URL로
`AsyncPostgresSaver`를 열어 API가 중단한 thread를 재개한다.

반드시 다음 조건을 만족해야 한다.

```text
LangGraph API의 AGENT_CHECKPOINT_DATABASE_URL
== Agent Worker의 AGENT_CHECKPOINT_DATABASE_URL
```

DB가 다르면 다음 현상이 발생한다.

- Executor는 `SUCCEEDED`
- Redis `execution.completed`도 발행됨
- Worker는 command를 받음
- Worker가 API에서 생성된 interrupt/checkpoint를 찾지 못해 resume 실패

## 6. Downgrade 주의사항

`ew_0001` downgrade는 모든 Worker namespace의 이벤트·command 데이터를 삭제한다.
그래서 명시적 승인 인자가 없으면 실패하도록 구현되어 있다.

```bash
alembic -x allow_worker_table_drop=true downgrade base
```

이 명령은 데이터 백업과 삭제 범위를 확인한 경우에만 사용한다. 운영 장애 복구에서
failed command 하나를 재처리하기 위해 schema downgrade를 사용하면 안 된다.

## 7. 운영 점검 항목

```sql
SELECT * FROM ew_alembic_version;

SELECT state, count(*)
FROM ew_commands
WHERE namespace = 'dtest-agent'
GROUP BY state;

SELECT command_id, execution_id, sequence, failure_attempts, last_error
FROM ew_commands
WHERE namespace = 'dtest-agent' AND state = 'FAILED'
ORDER BY updated_at DESC;
```

민감한 DB URL, 비밀번호, Redis credential은 문서나 migration 파일에 넣지 않고
Deployment Secret에서 환경변수로 주입한다.

