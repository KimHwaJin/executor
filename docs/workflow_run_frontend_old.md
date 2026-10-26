# Workflow Run 프론트 연동

화면은 그래프 노드 이름별로 만들지 않습니다. 기준은 Task snapshot 하나이고, 입력 폼은 snapshot의 `interactions`를 그대로 그립니다. 동작 중인 참고 화면은 `/demo_temp.html`입니다.

기본 prefix는 `/api/v1`입니다. 아래 경로의 UUID는 모두 서버가 만듭니다.

## 화면이 들고 있을 값

| 값 | 언제 생기나 | 어디에 쓰나 |
| --- | --- | --- |
| `user_id` | 사용자 이름 조회 | 모든 API의 `X-User-ID` |
| `task_id` | 첫 POST의 `Location` 또는 snapshot | 조회, SSE, 취소 |
| `session_id` | snapshot | 같은 대화에 다음 메시지를 넣을 때 |
| `last_event_sequence` | snapshot, SSE `id` | 재연결 `Last-Event-ID` |
| 작성 중인 폼 값 | 사용자가 입력 | 409가 나도 지우지 않음 |

Run ID는 화면이 만들어 저장하지 않습니다. 재개할 때는 열린 interaction의 `resume_target`을 그대로 복사합니다. 재개하면 새 Run이 생기지만 Task ID는 그대로입니다.

## 인증

로그인 API는 없습니다. 이름으로 사용자를 찾고, 이후 호출은 그 UUID만 헤더에 넣습니다.

```http
GET /api/v1/users/by-name/{user_name}
```

200 `UserRead`:

```json
{
  "user_id": "uuid",
  "user_name": "홍길동",
  "delete_yn": "N",
  "created_at": "2026-10-02T00:00:00Z",
  "updated_at": "2026-10-02T00:00:00Z",
  "deleted_at": null
}
```

없는 이름은 404입니다. 가입이 필요하면 아래를 사용합니다.

```http
POST /api/v1/users
Content-Type: application/json

{"user_name": "홍길동"}
```

201이며 body는 같은 `UserRead`입니다.

이후 화면 API:

```http
X-User-ID: <user_id>
```

헤더가 없거나 활성 사용자가 아니면 401입니다.

## 화면에서 쓰는 API

| 할 일 | Method | Path | 응답 |
| --- | --- | --- | --- |
| 새 메시지, 폼 제출 | POST | `/workflow_run` | 202 Task snapshot |
| 제출과 동시에 Task SSE | POST | `/workflow_run/stream` | 200 `text/event-stream` |
| 화면 복원 | GET | `/tasks/{task_id}` | 200 Task snapshot |
| 진행 구독 | GET | `/tasks/{task_id}/stream` | 200 `text/event-stream` |
| 취소 | POST | `/tasks/{task_id}/cancel` | 202 |
| 특정 Run 상세 | GET | `/workflow_run/{run_id}` | 200 |
| 특정 Run만 구독 | GET | `/workflow_run/{run_id}/stream` | 200 SSE. 화면 기본 경로는 아님 |

`/chat/completion`과 `POST /tasks/{task_id}/resume`은 쓰지 않습니다. 재개는 `POST /workflow_run`입니다.

## POST /workflow_run

헤더:

```http
X-User-ID: <user_id>
Idempotency-Key: <1~255자, 공백 아님>
Content-Type: application/json
```

새 메시지와 재개마다 새 키가 필요합니다. 같은 사용자, 같은 키, 같은 body면 기존 Run을 다시 돌려줍니다. 같은 키에 body가 다르면 409입니다.

body에 없는 필드를 넣으면 422입니다. `input`과 `command`는 둘 중 정확히 하나만 있습니다.

### 새 메시지

`task_id`, `run_id`는 넣지 않습니다.

```json
{
  "input": {
    "content": [{"type": "text", "text": "데이터 품질을 분석해줘"}]
  },
  "main_model_name": "default",
  "session_id": null,
  "project_id": null
}
```

- `content`는 비어 있지 않은 `text`만 가능합니다. 공백만 있는 문자열은 422입니다.
- `session_id`가 있으면 그 대화에 붙습니다.
- 없으면 `project_id` 또는 사용자 기본 프로젝트에 새 대화를 만듭니다. 기본 프로젝트도 없으면 409 `A default project is required.`입니다.
- `main_model_name`은 현재 `default`만 됩니다. 다른 값은 422입니다.

### 재개

`session_id`, `project_id`는 넣지 않습니다. 서버가 `run_id`로 대화를 찾습니다.

```json
{
  "main_model_name": "default",
  "task_id": "11111111-1111-4111-8111-111111111111",
  "run_id": "22222222-2222-4222-8222-222222222222",
  "command": {
    "resume": {
      "action": "approve",
      "interaction_id": "33333333-3333-5333-8333-333333333333",
      "revision": 1,
      "feedback": ""
    }
  }
}
```

`task_id`, `run_id`, `interaction_id`, `revision`은 `interactions[].resume_target`을 복사합니다. `action`은 그 interaction의 `actions[].name`만 보냅니다.

| action | 화면 | body |
| --- | --- | --- |
| `approve` | 승인 | `payload`를 넣지 않습니다. 서버 기본값이 승인됩니다. `feedback`은 코멘트일 뿐 입력값이 아닙니다. |
| `edit` | 입력 제출 | `payload`에 스키마대로 채운 객체를 넣습니다. |
| `respond` | 답변 | `feedback` 문자열, 또는 `payload`. |
| `reject` | 수정 요청 | `feedback` 문자열. |

`edit` 예:

```json
{
  "task_id": "11111111-1111-4111-8111-111111111111",
  "run_id": "22222222-2222-4222-8222-222222222222",
  "command": {
    "resume": {
      "action": "edit",
      "interaction_id": "33333333-3333-5333-8333-333333333333",
      "revision": 1,
      "feedback": "",
      "payload": {
        "target_column": "fail",
        "join_columns": ["lot_id"],
        "test_size": 0.2
      }
    }
  }
}
```

`payload` 키는 서버가 내려준 `input_schema.properties`를 따릅니다. 위 필드는 예시입니다. 스키마를 어기면 422이고, `readOnly` 필드를 바꾸면 422입니다. `approve`에 `payload`를 넣으면 422 `Use edit to change approved arguments.`입니다.

### 202 응답

헤더 `Location: /api/v1/tasks/{task_id}`. body는 아래 Task snapshot이고, 이번 제출의 Run이 `submission_run_id`입니다. 멱등 재전송으로 Task가 이미 진행됐으면 `current_run_id`와 다를 수 있습니다. GET과 SSE의 `submission_run_id`는 null입니다.

같은 대화에 `pending` 또는 `running` Task가 있으면 409 `An active task already exists for this session.`입니다.

## Task snapshot

`GET /api/v1/tasks/{task_id}`와 POST 202의 body입니다. 새로고침, 409 이후, SSE 재동기화 때 이 응답으로 화면을 다시 그립니다.

```json
{
  "schema_version": 1,
  "task_id": "uuid",
  "session_id": "uuid",
  "current_run_id": "uuid",
  "submission_run_id": null,
  "main_model_name": "default",
  "status": "waiting_input",
  "phase": "plan_review",
  "last_event_sequence": 18,
  "input_required": true,
  "task_finished": false,
  "current_run": {
    "id": "uuid",
    "status": "interrupted",
    "attempt_count": 1
  },
  "interactions": [],
  "capabilities": {"can_resume": true, "can_cancel": true},
  "result": null,
  "failure": null,
  "recovery": {"status": "none", "next_attempt_at": null, "user_action_required": false},
  "created_at": "2026-10-02T00:00:00Z",
  "updated_at": "2026-10-02T00:00:00Z",
  "started_at": "2026-10-02T00:00:00Z",
  "completed_at": null
}
```

`current_run`에는 시각, `interrupt`, `failure`, `cancel_reason`도 있습니다. 완료 판단은 `current_run.status`가 아니라 Task의 `status`와 `task_finished`로 합니다. Run이 `interrupted`여도 Task는 입력을 기다리는 중입니다.

`status`: `pending`, `running`, `waiting_input`, `success`, `error`, `timeout`, `canceled`.

| 화면이 볼 값 | 할 일 |
| --- | --- |
| `pending` 또는 `running`, `input_required: false` | 진행 표시. 채팅 입력 잠금 |
| `waiting_input`이고 `interactions`가 있음 | 그 항목으로 버튼과 폼 |
| `phase`가 `executor_wait` | 실행 결과 대기. 폼 없음. 취소 버튼 없음 |
| `success` | `result` 표시 후 종료 |
| `error`, `timeout` | `failure`와 `recovery` 표시 |
| `canceled` | 취소됨 |
| `capabilities.can_cancel` | 취소 버튼 |
| `capabilities.can_resume` | 폼 제출 가능. `interactions`가 있을 때 true |

`phase`는 열린 interaction의 `kind`이거나, Executor 대기면 `executor_wait`, 그 외에는 status 문자열입니다. 노드 이름으로 분기하지 않습니다.

## interactions

`waiting_input`일 때만 채워집니다. 항목 하나가 폼 하나입니다.

```json
{
  "interaction_id": "uuid",
  "revision": 1,
  "run_id": "uuid",
  "status": "open",
  "kind": "plan_review",
  "payload_version": 1,
  "summary": "실행 계획을 확인해 주세요",
  "payload": {
    "action_name": "workflow_approval",
    "arguments": {"workflow": {}},
    "input_schema": {"type": "object", "properties": {}}
  },
  "actions": [
    {"name": "approve", "label": "승인", "input_schema": {"type": "object"}},
    {"name": "edit", "label": "입력 제출", "input_schema": {}},
    {"name": "reject", "label": "수정 요청", "input_schema": {"type": "string"}}
  ],
  "resume_target": {
    "task_id": "uuid",
    "run_id": "uuid",
    "interaction_id": "uuid",
    "revision": 1
  }
}
```

그릴 때:

- 제목과 설명은 `summary`, 필요하면 `payload.action_name`.
- 버튼 글자는 `actions[].label`, 제출 값은 `actions[].name`. 목록에 없는 action은 보내지 않습니다.
- 입력 필드는 `edit`일 때 `payload.input_schema`입니다. 초기값은 `payload.arguments`입니다. 스키마가 비어 있거나 `""`인 필드는 화면 기본값을 넣되, 제출 값이 `minLength` 등을 만족해야 합니다.
- `kind`가 `plan_review`면 `payload.arguments.workflow`를 읽기 전용으로 보여 줍니다. 계획 정의는 수정하지 않습니다.
- `kind`가 `planning_question`이면 질문 폼입니다. 그 외 `kind`는 서버 action 이름 그대로입니다. 새 kind가 와도 스키마와 actions로 그리면 됩니다.
- 지금 revision은 1이고, 재개 뒤 다음 입력은 새 `interaction_id`입니다.

공개 목록에 나오지 않는 것:

- Executor에 넘긴 직후의 대기. 폼이 없고 `phase`는 `executor_wait`입니다. Task는 끝나지 않습니다.
- FAQ처럼 다음 말을 기다리는 내부 단계. 그 Task는 `success`로 끝나고, 다음 사용자 말은 새 Task입니다.

## 취소

`capabilities.can_cancel`일 때만 보여 줍니다.

```http
POST /api/v1/tasks/{task_id}/cancel
X-User-ID: <user_id>
Content-Type: application/json

{"reason": "사용자가 취소"}
```

`reason`은 생략 가능하고 최대 1000자입니다. 202이며 body는 운영용 Task 자원입니다. 화면 상태는 이 body가 아니라 snapshot 또는 SSE로 갱신합니다.

## SSE

브라우저 `EventSource`는 `X-User-ID`를 넣지 못합니다. `fetch`로 열고 `Content-Type`이 아니라 응답 `text/event-stream`을 읽습니다.

기본 구독:

```http
GET /api/v1/tasks/{task_id}/stream
X-User-ID: <user_id>
Last-Event-ID: 18
```

`Last-Event-ID`는 생략할 수 있습니다. 생략하면 현재 snapshot부터 이후 변경만 옵니다. GET으로 snapshot을 받은 뒤에는 그 `last_event_sequence`를 넣어, 조회와 구독 사이에 생긴 이벤트를 받습니다.

제출과 구독을 한 번에 하려면:

```http
POST /api/v1/workflow_run/stream
X-User-ID: <user_id>
Idempotency-Key: <새 키>
```

body는 `POST /workflow_run`과 같습니다. 응답 헤더:

```http
Location: /api/v1/tasks/{task_id}
X-Task-ID: <task_id>
X-Run-ID: <이번 run_id>
```

재개 후에도 Task SSE는 끊지 않습니다. 연결이 끊겨도 서버 작업은 취소되지 않습니다. 같은 Task URL로 다시 열고 마지막 sequence를 `Last-Event-ID`로 보냅니다.

프레임:

```text
id: 18
event: task.snapshot
data: {"schema_version":1,"event_id":null,"type":"task.snapshot","sequence":18,"session_id":"...","task_id":"...","run_id":"...","occurred_at":"...","data":{}}

: heartbeat
```

`data`는 envelope입니다. 화면 내용은 envelope의 `data`에 있습니다. `: heartbeat`로 시작하는 줄은 무시합니다.

| event | 화면 |
| --- | --- |
| `task.snapshot` | `data`가 GET snapshot과 같습니다. sequence가 같아도 화면 상태를 이 값으로 바꿉니다. |
| `task.queued`, `task.started`, `task.resumed`, `task.waiting_input`, `task.success`, `task.error`, `task.timeout`, `task.canceled`, `task.retry_scheduled` | 진행 상태. 최종 화면은 따라오는 snapshot을 따릅니다. |
| `interaction.opened` | `data`가 interaction 한 건입니다. 폼을 엽니다. |
| `interaction.submitted` | 그 `interaction_id` 폼을 닫습니다. 다른 탭에서 제출된 경우도 같습니다. |
| `message.completed` | 말풍선. 아래 참고. |
| `llm.token.delta` | 진단용입니다. 답변 말풍선으로 그리지 않습니다. |
| `agent.event` | 실행 로그입니다. 채팅 본문으로 쓰지 않습니다. |
| `stream.closed` | `reason`이 `task_finished`면 구독을 끝냅니다. `resync_required`면 GET snapshot으로 다시 맞춥니다. |
| `error` | `TASK_UNAVAILABLE`이면 Task를 더 이상 볼 수 없습니다. |

`message.completed`의 `data`:

```json
{
  "message_id": "uuid",
  "role": "assistant",
  "channel": "commentary",
  "content": [{"type": "text", "text": "계획을 만들고 있습니다."}]
}
```

`channel`이 `commentary`면 진행 안내, `final`이면 사용자에게 보이는 답변입니다. 같은 `message_id`는 한 번만 그립니다.

저장 이벤트는 `task_id`와 `sequence`로 중복을 제거합니다. `task.snapshot`만 예외로, 같은 sequence여도 적용합니다.

`Last-Event-ID`가 음수이거나 정수가 아니면 400입니다. 현재 `last_event_sequence`보다 크면 409이고 body detail에 `CURSOR_AHEAD`가 있습니다. 이벤트 공백이 있으면 `stream.closed` `resync_required`와 `snapshot_url`이 옵니다.

## GET /workflow_run/{run_id}

특정 Run의 상세입니다. 화면 복원에는 Task GET을 씁니다. 응답은 Run 필드에 상태 요약이 합쳐집니다.

```json
{
  "id": "uuid",
  "task_id": "uuid",
  "session_id": "uuid",
  "status": "waiting_input",
  "run_status": "interrupted",
  "stream_finished": true,
  "run_finished": false,
  "input_required": true,
  "interactions": [],
  "failure": null
}
```

이 `interactions`는 Task snapshot의 공개 폼과 형식이 다릅니다. 폼은 Task snapshot 것만 사용합니다. `GET /workflow_run/{run_id}/stream`은 그 Run의 status와 end만 보는 보조 스트림입니다.

## 화면 순서

1. 이름으로 `GET /users/by-name/{user_name}`을 호출하고 `user_id`를 저장합니다.
2. 사용자가 메시지를 보내면 새 `Idempotency-Key`로 `POST /workflow_run`을 호출합니다. 이어서 `Location`의 Task에 `GET /tasks/{task_id}/stream`을 엽니다. 또는 `POST /workflow_run/stream` 하나로 제출과 구독을 같이 합니다.
3. `task.snapshot`과 `message.completed`로 채팅을 갱신합니다. `input_required`가 아니면 입력을 잠급니다.
4. `interaction.opened` 또는 snapshot의 `interactions`로 폼을 그립니다. 제출은 `resume_target`과 선택한 `action`으로 `POST /workflow_run`입니다. 키는 새로 만듭니다. SSE는 유지합니다.
5. `phase`가 `executor_wait`이면 대기 표시만 합니다.
6. `task_finished`이거나 `stream.closed` `reason=task_finished`이면 구독을 닫고 `result` 또는 `failure`를 보여 줍니다.
7. 새로고침하면 저장해 둔 `task_id`로 GET snapshot을 받고, `last_event_sequence`로 SSE를 다시 엽니다.

409가 나면 사용자가 입력한 값은 유지하고 GET snapshot을 다시 받은 뒤, 새 `resume_target`으로 다시 보냅니다.

## 에러 body

실패는 `application/problem+json`입니다.

```json
{
  "type": "https://api.example.com/problems/COMMON-CONFLICT-001",
  "title": "Request conflict",
  "status": 409,
  "detail": "Run is stale or task is not waiting for input.",
  "instance": "/api/v1/workflow_run",
  "error_code": "COMMON-CONFLICT-001",
  "request_id": "req_..."
}
```

필드 검증 422에는 `errors` 배열이 붙습니다. `field`, `reason`, `message`가 있습니다. interaction 값 오류의 `detail`은 문자열이 아니라 `INVALID_INTERACTION_INPUT` 또는 `READ_ONLY_INPUT` 객체일 수 있습니다.

| status | 화면 |
| --- | --- |
| 401 | 사용자 UUID를 다시 조회 |
| 404 | 그 사용자에게 Task, Run, Session이 없음 |
| 409 | 작성 내용 유지 후 snapshot 재조회. 활성 Task가 있으면 새 메시지를 만들지 않음 |
| 422 | 폼 필드 오류 표시. 스키마와 `readOnly`를 확인 |
| 500 | 요청 실패 안내. `request_id`만 로그로 남김 |
