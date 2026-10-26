# Workflow Run / Task API

Task는 사용자 요청 전체를 나타내며 입력 대기·재개·완료까지 같은 ID를 유지합니다.
Run은 최초 실행 또는 사용자 재개로 생성되는 실행 구간입니다. 자동 재시도는 동일 Run의
attempt_count를 증가시킵니다. UUID는 서버가 생성합니다.

## 경로

| Method | Path | Response |
| --- | --- | --- |
| POST | `/api/v1/workflow_run` | 202: Task snapshot + submission_run_id |
| POST | `/api/v1/workflow_run/stream` | 200: 실행 수락 후 **Task 전체** SSE |
| GET | `/api/v1/tasks/{task_id}` | FE 화면 복원용 Task snapshot |
| GET | `/api/v1/tasks/{task_id}/stream` | FE 기본 구독. 재개로 Run이 바뀌어도 같은 연결 유지 |
| GET | `/api/v1/tasks/{task_id}/runs` | 해당 Task 실행 이력 |
| GET | `/api/v1/tasks/{task_id}/runtime` | 기존 잠금/lease/heartbeat 등 운영 조회 |
| GET | `/api/v1/workflow_run/{run_id}` | 특정 Run 상세 (기존 Run 응답 형식) |
| GET | `/api/v1/workflow_run/{run_id}/stream` | 특정 Run만 구독하는 보조 SSE (기존 status/end 형식) |
| POST | `/api/v1/tasks/{task_id}/cancel` | 전체 요청 취소 |

`X-User-ID: <활성 사용자 UUID>`만 사용합니다. 누락/잘못된 사용자면 401입니다.
POST 제출에는 공백이 아닌 1~255자 `Idempotency-Key`가 필요합니다.
같은 사용자/키/body 재전송은 기존 Run을 반환하며 REST와 POST SSE 사이에서도 공유됩니다.
키를 재사용하면서 body가 달라지면 409입니다. 신규 메시지와 각 재개에는 새 키를 사용합니다.
기존 `/chat/completion`은 deprecated 호환 경로로 유지합니다.

## 신규 입력

```json
{
  "input": {"content": [{"type": "text", "text": "데이터 품질을 분석해줘"}]},
  "main_model_name": "default"
}
```

선택 필드 session_id가 있으면 해당 대화를 사용합니다. 없으면 선택 필드 project_id 또는
사용자 기본 프로젝트에 새 대화를 만듭니다. 기본 프로젝트도 없으면 409입니다.
현재 content는 비어 있지 않은 text 파트를 지원합니다. 원본 파트를 메시지에 보존하며,
모델 입력에는 줄바꿈으로 연결한 텍스트를 사용합니다.
main_model_name은 확장용으로 저장·응답하고 재개 시 상속합니다. 현재 default만 지원하며
그 밖의 값은 422입니다. 실제 실행 모델 변경 기능은 아직 추가하지 않았습니다.

## Task snapshot

다음 ID 문자열은 설명용이며 실제로는 UUID입니다.

```json
{
  "schema_version": 1,
  "task_id": "T1",
  "session_id": "S1",
  "current_run_id": "R1",
  "submission_run_id": "R1",
  "main_model_name": "default",
  "status": "waiting_input",
  "phase": "plan_review",
  "last_event_sequence": 18,
  "input_required": true,
  "task_finished": false,
  "current_run": {"id": "R1", "status": "interrupted", "attempt_count": 1},
  "interactions": [],
  "capabilities": {"can_resume": true, "can_cancel": true},
  "result": null,
  "failure": null,
  "recovery": {"status": "none", "next_attempt_at": null, "user_action_required": false}
}
```

예시는 일부 Run 상세/시각 필드를 생략했습니다. 실제 waiting_input 응답의 interactions에는
아래 대기 항목이 포함됩니다. created_at/updated_at/started_at/completed_at는 Task 기준입니다.
POST의 submission_run_id는 이번에 수락/재사용한 Run입니다. 멱등 재전송 시 Task가 이미
진행했으면 current_run_id와 다를 수 있습니다. GET/SSE에는 submission_run_id가 null입니다.

status는 pending/running/waiting_input/success/error/timeout/canceled입니다.
Executor 대기 중에는 phase=executor_wait, input_required=false입니다.
Task 완료와 연결 종료는 별개입니다. run.interrupted만 보고 Task 완료로 처리하지 마세요.
실패/재시도 관련 정보는 recovery에 있고, 잠금 토큰은 공개하지 않습니다.

## 동적 입력과 재개

interactions의 각 항목에는 interaction_id, revision, run_id, status=open, kind,
payload_version, summary, payload, actions, resume_target이 있습니다.

- payload.action_name: 기존 도메인 동작 이름.
- payload.arguments: 기본값과 표시 자료.
- payload.input_schema: 입력 필드·타입·필수 값·readOnly·선택지 등의 JSON Schema.
- actions: 허용된 action name, 버튼 label, 입력 schema.
- resume_target: 다음 요청에 그대로 복사할 task_id/run_id/interaction_id/revision.

계획 검토(kind=plan_review)에는 payload.arguments.workflow로 현재 실행 정의를 제공합니다.
FE는 이 자료의 도구·단계·파라미터를 표시할 수 있습니다. 계획 정의는 읽기 전용이며,
PM 제안의 별도 plans/steps 이벤트 모델로 변환된 것은 아닙니다.

```json
{
  "task_id": "11111111-1111-4111-8111-111111111111",
  "run_id": "22222222-2222-4222-8222-222222222222",
  "command": {
    "resume": {
      "action": "approve",
      "interaction_id": "33333333-3333-5333-8333-333333333333",
      "revision": 1,
      "feedback": "이대로 진행해주세요"
    }
  }
}
```

run_id는 답변 대상인 기존 Run입니다. 새 Run ID는 서버가 생성하며 같은 Task에 연결합니다.
interaction ID는 저장된 Run/interrupt에서 안정적으로 파생됩니다. 현재 항목은 불변이며
revision=1입니다. 재개 후 새 interrupt는 새 ID를 받습니다.

approve는 기본값 승인, edit는 payload 객체 제출, respond는 자유 답변, reject는 수정 요청입니다.
현재 interaction의 actions에 제공된 결정만 사용하세요. 도메인 동작 이름도 edit/respond의
호환 별칭으로 받습니다. PM 예시의 answer_clarification 같은 이름을 임의로 보내지 마세요.
edit는 JSON 문자열 feedback도 호환 지원하지만 payload 객체 사용을 권합니다.
JSON Schema와 readOnly 필드 변경을 서버에서도 검사합니다. approve의 feedback은 감사용
metadata에 보존하며 승인된 인수로 사용하지 않습니다.
오래된 Run/interaction/revision, 동시 제출 충돌은 409입니다. FE는 작성 내용을 보존하고
Task snapshot을 다시 조회합니다. 새 클라이언트는 workflow_run으로 재개하세요.
기존 tasks/{id}/resume은 최신 Run을 대상으로 하는 낮은 수준의 호환 경로입니다.

## Task SSE

공통 envelope는 schema_version, event_id, type, sequence, session_id, task_id, run_id,
occurred_at, data를 가집니다. 저장 이벤트의 event_id는 UUID이고 합성 snapshot/종료는 null입니다.

| 이벤트 | 용도 |
| --- | --- |
| task.snapshot | GET과 같은 전체 화면 상태 |
| task.queued/started/resumed/waiting_input/success/error/timeout/canceled/retry_scheduled | 기존 실행 상태 변경 |
| interaction.opened | 입력/승인 UI 표시 |
| interaction.submitted | 다른 탭 포함, 제출된 폼 비활성화 |
| message.completed | 표시 메시지: message_id, role, channel, content |
| llm.token.delta | 기존 진단용 토큰. 공개 답변으로 직접 렌더링하지 않음 |
| stream.closed | reason=task_finished 또는 resync_required |

message.completed는 기존 공개 메시지 projection을 사용합니다. channel=commentary는 진행
안내, final은 최종 답변 계열입니다. 메시지와 이벤트는 같은 transaction으로 저장하며
중복 graph step 재시도는 message_id로 중복 이벤트를 만들지 않습니다.
공개 메시지 delta 및 별도 plan/step 이벤트는 후속 범위입니다.

Task SSE는 waiting_input에서도 연결을 유지합니다. resume POST 후에도 재구독이 필요 없습니다.
Task 종료 시 남은 이벤트와 snapshot을 보낸 뒤 stream.closed로 닫습니다.
연결 끊김은 취소가 아니며 Worker는 계속 실행합니다. idle에는 heartbeat 주석이 전송됩니다.

- cursor 없는 연결: 현재 snapshot at N을 보내고 N 이후 변경을 구독합니다.
- Last-Event-ID: N: N 이후 저장 이벤트를 먼저 재생하고 최신 snapshot을 적용합니다.
- snapshot은 마지막 저장 이벤트와 같은 sequence를 가질 수 있으므로 **task.snapshot은
  동일 sequence여도 적용**하세요. 저장 이벤트는 task_id+sequence로 중복 제거합니다.
- GET snapshot 후 last_event_sequence를 Last-Event-ID로 사용하면 조회/구독 사이 변경도 받습니다.
- GET cursor가 현재 최대값보다 크면 409 CURSOR_AHEAD입니다. 이벤트 유실 구간 또는 POST SSE의
  미래 cursor는 stream.closed reason=resync_required로 알립니다. snapshot/메시지 이력을 다시
  조회하세요. POST 실행 수락 여부는 X-Task-ID/X-Run-ID 헤더로 확인하고 같은 멱등 키로 재조회합니다.
- X-User-ID 헤더를 보낼 수 있는 fetch 기반 SSE 클라이언트를 사용하세요.

## 잠금과 검증

Session당 pending/running Task 한 개의 DB unique 제약, Worker SKIP LOCKED claim,
lock_token/heartbeat/lease 만료와 reconciler 재시도는 유지했습니다.
waiting_input에서 실행 lease를 풀고 재개 시 다시 점유하는 기존 정책도 같습니다.
FAQ/파일 조회 결과의 Task와 이벤트는 삭제하지 않고 종료 상태로 보존합니다.
FAQ의 next_user_request는 완료된 요청의 열린 승인 폼으로 노출하지 않습니다.
운영 잠금 정보는 tasks/{id}/runtime 및 기존 Session Task 목록에서 조회합니다.

```sh
PYTHONSAFEPATH=1 PYTHONPATH=src python3 -m unittest app.test.test_auth app.test.test_workflow_run_api app.test.test_workflow_task_stream -v
# 별도 테스트용 PostgreSQL URL. 매 테스트마다 임시 schema 생성/삭제.
WORKFLOW_TEST_DATABASE_URL=postgresql+asyncpg://... PYTHONSAFEPATH=1 PYTHONPATH=src python3 -m unittest app.test.test_workflow_task_postgres -v
```

실제 PostgreSQL에서 멱등 제출·Session 잠금·동시 재개·FAQ 보존·lease/복구·메시지 이벤트를
검증합니다. LLM/Executor 실제 실행, Pod 강제 종료·rolling deployment·규모별 부하는 별도
검증 범위이며 이번 변경으로 보장한다고 주장하지 않습니다.

## 브라우저 테스트 화면

채팅 형태의 통합 테스트는 서버 실행 후 `/demo_temp.html`을 엽니다. 기존 `/demo` 상단에도 이동 링크가 있습니다. 전송 방식 선택, HITL 재개, SSE 끊기·재연결, SessionLock 동시 요청, Heartbeat·Lease 조회를 지원합니다. Worker 중지와 Lease 만료는 외부에서 유도하고 이 화면에서 복구 상태를 관찰합니다.

별도 API 콘솔은 `/workflow-test`에서 사용할 수 있습니다.
X-User-ID 확인 → 요청 입력 → REST 또는 POST SSE 선택 → 실행 → 동적 폼 답변/승인 순서로
테스트할 수 있습니다. 기존 Task ID의 조회·재접속, 동일 멱등 키 재전송, Run/lock/lease 조회,
이벤트 원문 확인을 지원합니다. 복합 입력은 JSON 편집기로도 제출할 수 있습니다.
메시지 복원은 현재 Task Run들의 최근 Session 메시지 100건을 대상으로 합니다.
