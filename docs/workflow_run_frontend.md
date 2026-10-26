# Workflow Run 프론트 연동

> **API 경계 구현 업데이트 (2026-10-06):** v2 버전 보존·제출 검증·resume 변환을 구현했습니다.
> **에이전트 연결 상태:** 후보 선택·Tool input·계획 승인 모두 v2로 연결되었습니다. 추천 검색은 미구현입니다.
> 새 Workflow 1.0 선택부터 Tool input v2를 사용하고 기존 대기 checkpoint는 v1을 유지합니다.
> 에이전트 구현 범위는 [연결 현황](workflow_hitl_v2_agent_api_handoff.md#7-tool-input-노드-v2-연결)을 참고하세요.
> 정확한 적용 범위, F/E 기준, 검증 결과는 [API 검토·검증 결과](workflow_run_api_validation.md)를 우선 참고하세요.

> 구현 상태: 기존 본문은 현재 API 사용법입니다. 하단의 **HITL 3종 전달 계약 초안 (2026-10-06)**은
> 단계별 내부 계약입니다. 실제 연동 시 각 interaction의 payload_version을 따릅니다. 공통 API는 유지합니다.

전체 JSON 예시: [추천 후보 선택](examples/workflow_hitl_v2/01_recommended_selection.json),
[생성 후보 선택](examples/workflow_hitl_v2/01_generated_selection.json),
[Tool input 입력](examples/workflow_hitl_v2/02_tool_inputs.json),
[계획 승인](examples/workflow_hitl_v2/03_plan_review.json).
각 파일의 읽는 방법과 요청·응답 대응 관계는 문서 끝의 **전체 응답과 resume 요청 쌍**에 있습니다.

화면은 그래프 노드 이름별로 만들지 않습니다. 기준은 Task snapshot 하나이고, 입력 폼은 snapshot의 `interactions`를 그대로 그립니다. v2 참고 화면은 `/demo_v2.html`입니다. [브라우저 검증 결과](demo_v2_validation.md)를 참고하세요.

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
  "session_id": null,
  "project_id": null
}
```

- `content`는 비어 있지 않은 `text`만 가능합니다. 공백만 있는 문자열은 422입니다.
- `session_id`가 있으면 그 대화에 붙습니다.
- 없으면 `project_id` 또는 사용자 기본 프로젝트에 새 대화를 만듭니다. 기본 프로젝트도 없으면 409 `A default project is required.`입니다.
- 모델 설정은 서버의 `agent_config.py`에서 관리합니다. 요청에 모델 필드를 넣지 않습니다.

### 재개

`session_id`, `project_id`는 넣지 않습니다. 서버가 `run_id`로 대화를 찾습니다.

```json
{
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

## HITL 3종 전달 계약 초안 (2026-10-06)

**상태: API v2 처리와 후보 선택·Tool input·계획 승인 노드를 연결했습니다.**
사용자가 정한 범위는 워크플로우 선택, Tool input 입력, 계획 승인입니다.
필드명·버전·제출 방식은 이번 초안의 제안입니다. 버튼·카드·메뉴 등 화면 표현은 확정하지 않습니다.

### 공통 전달 및 제출

- 기존 `InteractionResource`와 REST/SSE envelope를 유지합니다.
- 이 초안은 `payload_version=2`를 제안합니다. 기존 version 1과 혼동하지 않습니다.
- REST `TaskSnapshot.interactions[]`와 SSE `interaction.opened.data`는 같은 interaction입니다.
- `payload.arguments`는 표시 자료, `payload.input_schema`는 제출값의 JSON Schema입니다.
  초안에서는 표시 자료 전체를 초기 제출값으로 복사하지 않습니다. 폼 초기값은 아래 필드별 규칙을 따릅니다.
- `actions`는 현재 interaction에서 전송 가능한 action을 나타냅니다. 이 v2 초안은 `approve/edit`를 사용합니다. 업무상 선택지는
  `payload.arguments.available_actions`로 제공합니다. 각 항목은 `intent`, `label`을 가집니다.
  버튼 사용을 강제하지 않으며 label이나 메시지 문구를 파싱해 분기하지 않습니다.
- 워크플로우 선택과 Tool input은 `action=edit`와 구조화된 `payload`를 사용합니다.
  계획 승인은 `approve`, 계획 거절은 `edit`와 `payload={"intent":"reject","reason":"..."}`를 사용합니다.
  두 거절의 필수 필드를 reason으로 통일하면서 공통 요청 모델을 유지하기 위한 매핑 제안입니다.
  기존 전송 action=reject는 feedback을 읽으므로 이 구조를 그대로 처리하지 못합니다.
  도메인 intent를 바깥쪽 action으로 보내지 않습니다. 위의 version 1 사용법은 변경하지 않습니다.
- `resume_target`의 네 필드를 복사합니다. `task_id/run_id`는 최상위,
  `interaction_id/revision`은 `command.resume`에 넣습니다.
- 재개 수락은 기존 REST `202 + TaskSnapshot`입니다. 처리 완료를 뜻하지 않으며 이후 SSE를 기다립니다.
  같은 Task SSE를 유지하고, 다음 interaction은 새 ID로 받습니다.

### 1. 워크플로우 선택

`kind=workflow_candidate_selection`, `payload.action_name=workflow_candidate_selection`을 사용합니다.
추천·생성은 같은 HITL이며 후보별 `origin`으로 구분합니다. 선택은 실행 승인이 아닙니다.
추천은 최초 검색에서 최대 `WORKFLOW_RECOMMENDATION_LIMIT`개(기본 3)를 제공합니다.
재추천 응답에는 **기존 후보 + 이번 검색의 신규 후보** 전체가 포함되므로 누적 목록은 3개를 넘을 수 있습니다.
생성·재생성은 `WORKFLOW_GENERATION_LIMIT`개(기본 2, 허용 1..20)를 목표로 합니다.
추가 후보가 재시도 후에도 중복이면 이미 생성된 유효 후보만 제공할 수 있습니다.
이때 기존 `candidates[].reason`과 에이전트 메시지에 요청 개수·실제 개수 및 중복 안내를 전달합니다.
중복 후보를 표시하지 않으며, 프론트는 reason을 숨기지 않고 실제 후보 수로 렌더링합니다.
프론트는 추천·생성 모두 후보 수를 고정하지 않고 서버의 배열을 렌더링합니다.
거절 대상은 현재 interaction의 후보 전체이며, 거절 제출에는 candidate_id를 넣지 않습니다.

`payload.arguments`의 제안 필드:

| 필드 | 타입 | 의미 |
| --- | --- | --- |
| `candidates` | array | 현재 선택 가능한 후보. 없으면 빈 배열 |
| `candidates[].candidate_id` | string | 서버가 부여한 후보 식별자. 화면 정렬 순서와 무관 |
| `candidates[].origin` | `recommended` / `generated` | 후보 출처 |
| `candidates[].name` | string, 선택 | 화면 표시용 워크플로우 이름. 식별·선택·분기에는 사용하지 않음 |
| `candidates[].description` | string | 후보 설명 |
| `candidates[].reason` | string | 추천 이유 또는 생성 설명 |
| `candidates[].workflow_id` | string(UUID), 선택 | 추천 후보의 카탈로그 식별자 |
| `candidates[].version_id` | string(UUID), 선택 | 추천 후보가 참조하는 저장 버전 식별자 |
| `candidates[].revision` | integer, 1 이상, 선택 | 저장된 워크플로우의 버전 번호 |
| `candidates[].similarity_score` | number, -1..1, 선택 | 추천 후보의 코사인 유사도 |
| `candidates[].workflow` | object | 후보의 Workflow 문서 전체 |
| `recommendation_search` | object, 선택 | 추천 검색 결과. 생성 후보·이전 응답에서는 생략 가능 |
| `recommendation_search.status` | `matched` / `no_additional_matches` / `no_matches` | 이번 검색 결과 상태 |
| `recommendation_search.added_candidate_ids` | string[] | 이번 응답에서 새로 추가된 후보 ID. 후보 배열 전체의 ID가 아님 |
| `recommendation_search.message` | string 또는 null | 추가 후보 없음 등의 표시용 안내 |
| `available_actions` | array | 현재 처리 가능한 intent와 표시 label |

아래 예시는 `payload.arguments` 일부입니다. `workflow`는 이 예시에만 생략했으며 실제 응답에는 전체 문서가 필요합니다.

```json
{
  "candidates": [
    {
      "candidate_id": "candidate_1",
      "origin": "recommended",
      "name": "데이터 품질 확인",
      "description": "결측과 기본 통계를 확인합니다.",
      "reason": "요청한 데이터 품질 분석에 해당합니다.",
      "workflow_id": "55555555-5555-4555-8555-000000000001",
      "version_id": "66666666-6666-4666-8666-000000000001",
      "revision": 2,
      "similarity_score": 0.942
    }
  ],
  "recommendation_search": {
    "status": "matched",
    "added_candidate_ids": ["candidate_1"],
    "message": null
  },
  "available_actions": [
    {"intent": "select", "label": "선택"},
    {"intent": "recommend_again", "label": "다시 추천"},
    {"intent": "generate", "label": "워크플로우 생성하기"},
    {"intent": "reject", "label": "이유와 함께 거절"}
  ]
}
```

추천 목록의 표시 규칙:

- `matched`: 응답의 누적 `candidates`로 목록을 갱신합니다. `added_candidate_ids`에 포함된 항목에만 **새 후보** 표시를 합니다. 최초 추천에서는 모든 후보 ID가 포함될 수 있습니다.
- `no_additional_matches`: 기존 후보를 그대로 표시하고 `message`를 목록 위에 안내합니다. 새 후보 ID 배열은 비어 있습니다. 자동으로 생성으로 전환하지 않으며 기존 후보 선택과 서버가 제공한 `generate` 행동을 유지합니다.
- `no_matches`: 최초 검색에 적합한 후보가 없다는 내부 결과입니다. 현재 에이전트는 곧바로 생성 경로로 진행하므로 일반적으로 다음 선택 화면에는 생성 후보가 제공됩니다.
- 신규 후보 여부는 이전 목록과의 단순 비교나 배열 순번이 아니라 **현재 응답의** `added_candidate_ids`로 판단합니다. 다음 재추천 응답에서 기존 배지를 해제합니다.
- 유사도는 예를 들어 `유사도 0.942`, 저장 버전은 `워크플로우 버전 2`처럼 표시할 수 있습니다. 값이 없으면 숨기고 0점이나 임의 버전으로 채우지 않습니다. 생성 후보에는 유사도가 없습니다.
- `candidates[].revision`은 DB 문서 버전이며, `interaction.revision` 및 `resume_target.revision`과 별개입니다. resume에는 항상 새 interaction의 `resume_target`을 복사합니다.
- 서버가 현재 후보 전체를 보내므로 이전 응답을 프론트에서 다시 덧붙이면 중복될 수 있습니다. 표시 순서나 개수를 조절하더라도 선택에는 원래 `candidate_id`를 사용합니다.

모든 선택 행동은 `action=edit`로 제출합니다. 아래 객체가 `command.resume.payload`입니다.

| intent | payload 예시 | 의미 |
| --- | --- | --- |
| `select` | `{"intent":"select","candidate_id":"candidate_1"}` | 제공된 특정 후보 선택 |
| `recommend_again` | `{"intent":"recommend_again"}` | 기존 후보를 검색에서 제외해 신규 후보를 찾고, 기존 + 신규 후보를 누적 반환 |
| `generate` | `{"intent":"generate"}` | 추천 후보 선택 대신 사용자 요청을 바탕으로 워크플로우 생성으로 전환 |
| `regenerate` | `{"intent":"regenerate"}` | 기존 생성 후보 전체를 제외하고 설정된 개수의 새 후보로 교체. 특정 후보 수정 아님 |
| `reject` | `{"intent":"reject","reason":"분석 목적과 맞지 않음"}` | 이유와 함께 현재 후보 목록 거절 |

재추천·재생성은 현재 피드백을 수집하지 않으며 위 예시처럼 intent만 제출합니다.
향후 수집을 위해 해당 intent의 스키마에 선택 문자열 `feedback`을 정의하되, 현재 화면에는 입력을 요구하지 않습니다.
필드가 생략되면 피드백 없는 요청으로 처리합니다. 향후 수집 시에도 공통 envelope는 바꾸지 않습니다.
`reject.reason`은 공백이 아닌 필수 문자열입니다.
`regenerate`는 기존 생성 후보를 채택하지 않은 경우 새 후보를 만드는 행동이며, 대상 candidate_id는 받지 않습니다.
추천에서 생성으로 전환하는 동작은 generate입니다. 추가 입력 없이 intent만 제출하며 regenerate와 구분합니다.
생성 후보 화면의 available_actions 예시는 select/regenerate/reject이고, 위 추천 예시는 select/recommend_again/generate/reject입니다.
generate는 추천 후보를 재생성 제외 이력에 넣는 동작이 아닙니다. 사용자 요청을 유지한 채 생성 경로로 전환합니다.
후보 거절 이유에 따른 다음 단계는 에이전트가 판단합니다. 새 워크플로우를 원하는 것인지,
분석 자체를 중단하려는 것인지도 이유와 대화 맥락으로 판단합니다.
프론트는 reason만 제출하며 다음 노드나 경로를 지정하지 않습니다. 후보 전체 거절이 곧 분석 취소를 뜻하지 않습니다.
후보 name은 선택적인 표시 편의 필드입니다. 없어도 candidate_id로 선택할 수 있어야 하며,
화면은 설명이나 표시 순번을 사용할 수 있습니다. 추후 필드를 제거할 수 있으나 연동 후 제거 시에는 프론트와 호환성을 확인합니다.
추천/생성 출처만 보고 행동을 고정하지 않고 서버의 `available_actions`를 따릅니다.
후보가 없으면 `select`를 제공하지 않습니다. 제공하지 않은 intent는 서버도 거부해야 합니다.

선택 요청의 전체 예시:

```json
{
  "task_id": "11111111-1111-4111-8111-111111111111",
  "run_id": "22222222-2222-4222-8222-222222222222",
  "command": {
    "resume": {
      "action": "edit",
      "interaction_id": "33333333-3333-5333-8333-333333333333",
      "revision": 1,
      "payload": {"intent": "select", "candidate_id": "candidate_1"}
    }
  }
}
```

`input_schema`는 intent별 `oneOf` 객체로 생성합니다. 각 객체는 `additionalProperties=false`,
`intent.const`, 행동별 필수 필드를 가집니다. `select.candidate_id`는 현재 후보 ID의 enum으로 제한합니다.
`available_actions`와 스키마의 허용 intent는 동일한 서버 정의에서 생성해야 합니다.
표시된 후보의 내용은 해당 interaction 동안 불변이며, 후보 변경 시 새 interaction을 발행합니다.
재추천은 기존 추천 workflow_id를 검색 대상에서 제외하지만, 기존 후보는 응답 목록에 유지합니다.
재생성은 같은 Task의 이전 생성 후보 실행 구조를 제외하고 새 후보 묶음으로 교체합니다.
현재 생성 중복 검사는 steps와 execution_mode의 구조로 비교하며 이름·actor만 바꾸는 경우는 같은 후보입니다.
제외 이력은 서버가 관리하므로 클라이언트가 제외 목록을 제출하지 않습니다.
추가 추천이 없으면 기존 후보를 새 후보로 표시하지 않고 안내와 기존 목록을 유지합니다.

### 2. Tool input 입력

`kind=additional_information`, `payload.action_name=additional_information`을 유지합니다.
기존 미해결 입력 전용 폼과 달리, **워크플로우의 각 도구 호출에 대해 필수·선택 인자를 모두** 전달합니다.
워크플로우 문서와 입력 표시 자료를 분리합니다.

| `payload.arguments` 필드 | 타입 | 의미 |
| --- | --- | --- |
| `workflow_id` | string | 입력 대상 워크플로우 식별자 |
| `workflow_revision` | integer, 1 이상 | 이번 입력 화면이 참조하는 문서 버전 |
| `workflow` | object | 전체 워크플로우 문서 |
| `tool_inputs` | array | 도구 호출별 표시 자료 |
| `tool_inputs[].step_index` | integer, 0 이상 | `workflow.workflow.steps`의 위치. 이 step의 skill을 식별 |
| `tool_inputs[].skill` | string | 표시할 skill 이름 |
| `tool_inputs[].tool_index` | integer, 0 이상 | 해당 step의 `tools` 안에서 위치 |
| `tool_inputs[].tool` | string | 표시할 tool 이름 |
| `tool_inputs[].parameters` | array | 해당 호출의 필수·선택 인자 전체 |
| `available_actions` | array | `{"intent":"submit_inputs","label":"입력 제출"}` |

같은 skill/tool이 여러 번 나올 수 있으므로 이름만으로 대상을 찾지 않습니다.
API index는 **0부터 시작**하며, 화면은 필요하면 1부터 표시합니다. 순서가 바뀌면 workflow_revision도 바뀝니다.

각 `parameters[]` 항목의 필드:

| 필드 | 타입 | 의미 |
| --- | --- | --- |
| `name`, `title`, `description` | string | 인자 식별자와 표시 설명 |
| `required` | boolean | 실행에 값 또는 유효한 연결이 필요한지 |
| `editable` | boolean | 사용자가 값을 제출할 수 있는지 |
| `value_schema` | object | 타입·enum·범위·nullable 등을 담은 JSON Schema |
| `has_default` | boolean | 기본값 존재 여부 |
| `default_value` | JSON value | has_default=true일 때만 존재 |
| `has_value` | boolean | 현재 표시할 실제 값 존재 여부 |
| `value` | JSON value | has_value=true일 때만 존재 |
| `value_origin` | enum | `user_request`, `default`, `user_answer`, `unset`, `workflow_literal`, `tool_output` |
| `binding` | object, 선택 | 이전 tool 출력 등 기존 워크플로우의 연결 정보 |

초기값 규칙:

| 인자 | 사용자 발화에서 에이전트가 추출한 값 있음 | 추출한 값 없음 |
| --- | --- | --- |
| 필수 | 추출한 값, `value_origin=user_request` | `has_value=false`, `value_origin=unset`, 빈 입력 |
| 선택 | 추출한 값, `value_origin=user_request` | 기본값이 있으면 기본값과 `value_origin=default` |

이 계약에서 선택 인자는 도구에 기본값이 정의된 인자입니다. 기본값은 None 또는 다른 값이며,
선택 인자는 `has_default=true`와 `default_value`를 항상 제공합니다. None은 JSON의 null입니다.
기본값은 서버가 도구 정의에서 읽어 제공하며 프론트가 추정하지 않습니다.
에이전트가 사용자 발화 근거 없이 추측한 값은 `user_request`로 표시하지 않습니다.
`has_value=false`는 value 키 생략이고, `value=null`은 nullable 인자의 실제 값입니다.
`false`, `0`, 빈 문자열도 존재하는 값이며 허용 여부는 value_schema가 결정합니다.

이전 tool 출력 참조와 workflow literal 인자도 빠짐없이 표시합니다. 수정 가능 여부는 각 인자의 `editable`을 따릅니다.
출력 참조는 `binding`과 `value_origin=tool_output`을 제공하고 아직 실행 전이면 `has_value=false`입니다.
이는 사용자 입력 누락과 다릅니다. 큰 데이터 객체를 직접 폼에 넣지 않습니다.
현재 v2 구현은 workflow literal을 수정 가능하게 제공합니다. `value_origin=workflow_literal`이라는 이유로 읽기 전용 처리하지 않습니다. 이전 tool 출력 연결도 `editable=true`로 제공하며 변경할 수 있습니다. 아래 「Tool 출력 연결 편집」 규격을 따릅니다.

도구 입력 표시 예시 (`tool_inputs`의 한 항목, 설명용 도구):

```json
{
  "step_index": 0,
  "skill": "example_skill",
  "tool_index": 1,
  "tool": "example_tool",
  "parameters": [
    {
      "name": "column", "title": "대상 컬럼", "description": "분석할 컬럼",
      "required": true, "editable": true,
      "value_schema": {"type": "string", "minLength": 1},
      "has_default": false, "has_value": false, "value_origin": "unset"
    },
    {
      "name": "method", "title": "분석 방법", "description": "사용자가 요청한 방법",
      "required": false, "editable": true,
      "value_schema": {"type": "string", "enum": ["iqr", "zscore"]},
      "has_default": true, "default_value": "iqr",
      "has_value": true, "value": "zscore", "value_origin": "user_request"
    },
    {
      "name": "threshold", "title": "기준값", "description": "탐지 기준",
      "required": false, "editable": true,
      "value_schema": {"type": "number", "exclusiveMinimum": 0},
      "has_default": true, "default_value": 3,
      "has_value": true, "value": 3, "value_origin": "default"
    }
  ]
}
```

제출은 `action=edit`이며 다음이 `command.resume.payload`입니다.

```json
{
  "intent": "submit_inputs",
  "workflow_id": "workflow_1",
  "workflow_revision": 1,
  "inputs": [
    {"step_index": 0, "tool_index": 1, "parameter": "column", "value": "yield"},
    {"step_index": 0, "tool_index": 1, "parameter": "method", "value": "zscore"},
    {"step_index": 0, "tool_index": 1, "parameter": "threshold", "value": 3}
  ]
}
```

제출값 규칙: 값이 있는 editable 인자를 기본값·미수정 값까지 포함해 모두 제출합니다.
사용자 발화에서 채운 값과 기본값은 수정 가능하며, 이전 tool 출력 연결도 수정할 수 있습니다.
값이 없는 선택 인자는 생략하고, 값이 없는 필수 editable 인자가 있으면 제출할 수 없습니다.
`editable=false` 인자는 제출하지 않으며 서버의 기존 연결을 유지합니다.
선택 인자를 비우면 도구에 정의된 기본값으로 되돌립니다. 미입력 또는 값을 지워 제출 목록에서 생략한 경우,
서버가 해당 도구의 기본값을 적용합니다. 사용자 발화로 채웠던 기존 값도 유지하지 않고 기본값으로 되돌립니다.
기본값이 None이면 None(JSON null), 다른 값이면 해당 값을 적용합니다.
값을 지우는 것은 null 제출과 다릅니다. null은 스키마에서 허용할 때만 사용할 수 있습니다.
클라이언트는 source/origin을 제출하지 않으며, 서버가 저장된 초기값과 제출값을 비교해 출처·확인을 기록합니다.

서버는 workflow ID/버전, `(step_index, tool_index, parameter)`의 존재·중복·수정 권한,
인자 타입과 필수값을 확인합니다. 이후 저장된 워크플로우에 입력을 반영하고 컴파일 검증한 뒤 계획 승인으로 넘깁니다.
재사용 정의와 execution_inputs를 별도로 유지할 수 있으므로, 완성된 워크플로우가 모든 입력의 literal 삽입을 뜻하지는 않습니다.

### 3. 계획 승인

`kind=plan_review`, `payload.action_name=workflow_approval`을 유지합니다.
`payload.arguments`에 `workflow_id`, `workflow_revision`, 전체 `workflow`, 확정된
`execution_inputs`를 제공합니다. execution_inputs 항목은 step_index/tool_index/parameter/value를 포함합니다.
화면은 두 자료를 합쳐 실제 실행할 도구와 확정 입력값을 읽기 전용으로 보여줍니다.
출력 참조는 연결 정보로 표시합니다. 이 단계의 사용자 행동은 승인 또는 이유와 함께 거절입니다.

| 행동 | `command.resume`의 추가 필드 | 의미 |
| --- | --- | --- |
| 승인 | `"action":"approve"` | interaction에 저장된 정확한 계획과 입력값 승인. payload 없음 |
| 거절 | `"action":"edit", "payload":{"intent":"reject","reason":"이상치 제거 단계는 원하지 않습니다"}` | 공백이 아닌 필수 reason. 후보 거절과 같은 제출 구조 |

두 요청 모두 공통 interaction_id/revision이 필요합니다. 승인할 workflow를 클라이언트가 다시 보내지 않습니다.
서버는 interaction에 묶인 workflow_revision이 현재 계획과 같은지 확인하고 오래된 승인은 거부해야 합니다.
Tool input 제출 후에는 제출값과 미입력 선택 인자의 기본값을 반영하고 검증만 수행합니다.
에이전트는 도구·단계·입력값을 추가로 변경하지 않습니다. 검증에 실패하면 값을 임의로 고치지 않고 입력 오류를 안내합니다.
승인 화면은 읽기 전용이며, 사용자는 완성된 계획을 확인하고 승인하거나 이유와 함께 거절합니다.
승인되면 화면에 표시한 워크플로우와 입력값 그대로 실행합니다. 에이전트의 자동 계획 변경·재승인 루프는 이번 범위에 포함하지 않습니다.
거절 이유를 보고 에이전트가 계획 자체에 대한 거절인지 분석 요청 자체의 중단 의사인지 판단해 다음 단계로 분기합니다.
계획 검토의 actions는 approve/edit를 제공하고, edit 입력 schema는 intent=reject와 공백이 아닌 필수 reason만 허용합니다.
프론트는 거절 시 payload.reason을 제출하고 거절만으로 Task를 종료하지 않습니다.
의도가 모호할 때 확인을 요청하는 처리를 제안하며, 확인 interaction의 상세 규격과 구체적인 분기 목록은 별도 설계 대상입니다.
거절된 계획은 실행하지 않습니다. 에이전트의 중단 판단은 API의 기존 취소/종료 처리와 연결해야 하며
새로운 Task 상태를 만들거나 프론트가 임의로 취소 API를 호출하는 의미는 아닙니다.

### SSE 메시지 전달 규칙

이벤트 종류와 envelope는 추가하지 않습니다. 아래는 현재 코드가 전달하는 실제 문구입니다.
`{...}`는 실행마다 채워지는 값입니다. 문구 변경은 가능하며 프론트는 문구 문자열을 분기 조건으로 사용하지 않습니다.
모든 채팅 메시지는 `message.completed`로 전달됩니다.

| 시점 | 채널 | 실제 문구 |
| --- | --- | --- |
| 후보 준비 | `commentary` | 사용자 요청에 맞는 워크플로우 후보를 준비하고 있습니다. |
| 생성 완료 | `commentary` | 요청에 맞는 워크플로우 후보를 생성했습니다. 후보를 확인하고 선택해 주세요. |
| 후보 목록 추가 | `commentary` | {새로 생성한/추천된} Workflow 후보 `{candidate_id}`를 목록에 추가했습니다. 상태: {상태}. |
| 입력 반영 완료 | `commentary` | 입력값을 반영했습니다. 실행 계획을 확인해주세요. |
| 입력 오류 | `commentary` | 입력값을 다시 확인해주세요. |
| 실행 코드 준비 | `commentary` | 승인된 계획의 실행 코드를 준비했습니다. |
| 실행 요청 제출 | `commentary` | 분석 워크플로우를 실행기에 제출했습니다. |
| 자동 제출 비활성 | `commentary` | 실행기 요청 파일을 생성했으며 자동 제출은 생략했습니다. |
| 후속 코드 준비 | `commentary` | 다음 분석 단계의 실행 코드를 준비했습니다. |
| 후속 실행 요청 제출 | `commentary` | 다음 분석 단계를 실행기에 제출했습니다. |
| 후속 자동 제출 비활성 | `commentary` | 다음 분석 단계의 실행 요청을 준비했으며 자동 제출은 생략했습니다. |
| 분석 종료 요청 | `commentary` | 실행기에 분석 종료를 요청했습니다. 완료 응답을 기다리고 있습니다. |
| 종료 자동 제출 비활성 | `commentary` | 분석 종료 요청을 준비했으며 자동 제출은 생략했습니다. |
| 오류로 인한 중단 요청 | `commentary` | 분석 단계에서 오류가 발생해 실행 중단을 요청했습니다. |
| 중단 자동 제출 비활성 | `commentary` | 오류로 인한 실행 중단 요청을 준비했으며 자동 제출은 생략했습니다. |
| 실행 성공 | `final` | 분석 실행이 완료되었습니다. |
| 성공 응답에 실패 단계 존재 | `final` | 분석 실행이 완료되었습니다. 일부 분석 단계에서 실패 {건수}건이 있습니다. |
| 오류로 인한 취소 완료 | `final` | 분석 단계 오류로 실행이 중단되었습니다. |
| 일반 취소 완료 | `final` | 분석 실행이 취소되었습니다. |
| 실행 실패 | `final` | 분석 실행에 실패했습니다. |
| 기타 종료 상태 | `final` | 분석 실행이 {status} 상태로 종료되었습니다. |
| 보고서 생성 생략 | `final` | 실패하거나 완료되지 않은 실행 셀이 있어 분석 리포트 생성을 생략했습니다. |
| 사용자 요청 종료 | `final` | 사용자 요청으로 현재 작업을 종료했습니다. |
| 보고서 완성 | `final` | 실제 실행 결과를 바탕으로 LLM이 작성한 보고서 본문 |

후보 상태는 needs_input이면 `추가 정보 필요`, ready이면 `승인 준비 완료`입니다. 상태가 없으면 상태 문장을 생략합니다.
실행 종료 메시지에 오류 내용이 있으면 ` 원인: {오류 내용}`을 덧붙이며, 여러 원인은 ` | `로 구분합니다.
추천 검색은 현재 연결되지 않았으므로 추천 시작 문구를 현재 전송 문구로 확정하지 않습니다.

### 실행 실패 표시와 대기 종료

실행기 실패 결과도 Worker가 처리한 뒤 기존 SSE로 전달합니다. 새 SSE 타입은 필요하지 않습니다.

1. `message.completed`의 `channel=final` 메시지로 실행 실패 원인을 표시합니다. 이 메시지만으로 Task 종료를 판단하지 않습니다.
2. `task.error`를 반영하고, 최종 `task.snapshot`의 `status=error`, `task_finished=true`에 따라 실행 중 표시를 종료합니다. 연결을 다시 열거나 GET으로 조회해도 동일한 실패 상태를 복원합니다.
3. `stream.closed`의 `reason=task_finished`를 받으면 SSE 연결을 닫습니다. 연결 종료 자체를 실행 성공으로 해석하지 않습니다.

`failure.message`는 실패 안내에 사용할 수 있으며, `failure.execution_status`는 실행기 상태입니다.
실행기 상태가 `SUCCEEDED`여도 결과 파일 수집 실패 등으로 Task가 `error`일 수 있으므로 화면 상태는 Task 기준으로 표시합니다.
MULTI의 중간 결과는 `task_finished=false`를 유지하며, 다음 실행을 기다리는 동안 로딩을 종료하지 않습니다.
성공과 일반 취소는 각각 `task.success`, `task.canceled` 및 최종 snapshot으로 처리합니다.

[04_execution_failed.json](examples/workflow_hitl_v2/04_execution_failed.json)은 실패 메시지부터 SSE 종료까지의 예시입니다.
`sse_events`는 설명을 위해 여러 envelope를 모은 배열이며 실제 전송 시에는 각 이벤트가 별도 SSE frame입니다.

조건부 판단은 `commentary`로 다음 템플릿을 전달합니다. 각 판단에 대해 항목을 반복하며 결과와 이유는 동적입니다.

```text
조건부 Tool 판단 결과

- 조건 Tool: {조건 Tool ID} ({상태})
- 조건 결과 요약: {요약}
- 판단 대상: {Tool ID}
- 결정: {판단 결과}
- 이유: {이유}

- 다음 실행 Tool: {Tool ID 목록 또는 없음}
```

HITL 화면 안내는 채팅 메시지가 아닌 `interaction.opened`의 `summary`로 전달합니다.

| 화면 | 실제 안내 문구 |
| --- | --- |
| 후보 선택 | 워크플로우 후보를 확인하고 선택해 주세요. |
| Tool input | 각 Tool의 필수·선택 인자를 확인하고 입력해주세요. Step/Tool 번호는 0부터 시작합니다. |
| 입력 오류 후 재표시 | 위 입력 안내 뒤 줄바꿈과 `이전 입력 오류: {오류 내용}` 추가 |
| 계획 승인 | 완성된 워크플로우와 입력값을 확인하고 실행을 승인해주세요. |

실행 결과와 보고서가 각각 `final`로 전달될 수 있으므로 final 한 건을 Task 종료로 간주하지 않습니다.
코드 위치: `graphs/nodes`의 각 메시지, `services/chat_crud_message_sink.py`의 문구 투영,
`services/graph_event_persistence.py`의 화면 메시지 전달 경로입니다.

commentary는 background 진행 안내, final은 사용자 답변 영역에 표시합니다.
HITL 안내 문장은 `summary`로 제공하므로 별도 채팅 메시지는 필수가 아닙니다.
문장으로 폼 개폐나 실행 완료를 판단하지 않습니다. final 메시지만으로 Task 종료를 판단하지도 않습니다.
승인 수락 직후 아직 큐 대기라면 실행 중이라고 안내하지 않습니다.

제출 중에는 해당 폼의 중복 제출을 막습니다. `interaction.submitted` 또는 새 snapshot으로 수락을 반영합니다.
422이면 입력을 보존하고 오류를 표시합니다. 409이면 입력을 보존한 채 snapshot을 다시 조회하며 자동 재제출하지 않습니다.
통신 실패로 수락 여부가 불명확하면 같은 멱등 키와 같은 body로 재확인합니다.
새로고침 시에도 snapshot만으로 현재 interaction을 복원할 수 있어야 하며 이전 commentary에 의존하지 않습니다.
SSE 순서·중복 제거·재연결 규칙은 위의 기존 공통 계약을 그대로 따릅니다.

### 확정 범위와 남은 결정

- 사용자 요구로 확정: HITL 3종, 후보의 추천/생성 출처, 선택/재추천/재생성/이유 있는 거절,
  tool 필수·선택 인자 전체 표시와 초기값 규칙, skill/tool 위치를 포함한 입력 제출, 완성된 계획 승인/이유 있는 거절.
  재추천은 검색에서 기존 후보 제외 후 누적 표시, 재생성은 설정된 개수의 새 후보로 교체, 현재 재시도 피드백 미수집(향후 확장),
  두 거절 모두 reason 필수이며, 후보 거절은 현재 목록 전체에 적용합니다.
  두 거절 단계 모두 에이전트가 이유를 해석해 워크플로우 관련 요청인지 분석 중단인지 판단하고 다음 경로를 정합니다.
- 사용자 확인: 발화 기반 값과 기본값은 수정 가능, 이전 tool 출력 연결도 수정 가능,
  값이 있는 입력 가능 인자 전체 제출, workflow_id/버전 및 0 기반 step_index/tool_index/parameter로 대상 식별.
- 이 문서의 제안: payload_version 2, 나머지 필드명, intent와 전송 action 매핑,
  미래 feedback 선택 필드, Task 단위 제외 이력, 모호한 거절 이유 확인. workflow literal은 현재 구현에서 editable 값에 따라 수정 가능합니다.
- 미정: 이전 후보의 화면 이력 표시, 후보 제외 이력의 유지 범위와 생성 후보 동일성 기준,
  이유에 따른 구체적인 분기·중단 처리와 확인 규격, 피드백 수집 UI 도입 시점,
  출처별 추가 허용 행동 범위와 UI 표현. 미정 경로는 실제 배포 전에 합의하고 처리 가능한 행동만 노출합니다.
- 기존 version 1의 기다리는 interaction은 원래 규격으로 처리해야 합니다.
  프론트는 실제 payload_version을 기준으로 화면과 제출값을 구성합니다. 기존 입력 대기 건의 v1 호환은 유지합니다.

## 전체 응답과 resume 요청 쌍

**v2 계약 예시**입니다. 전체 JSON은 생략 기호 없이 별도 파일로 제공합니다.
이 예시들은 서버의 실제 응답을 수집한 것이 아니며, 등록되지 않은 설명용 demo skill/tool을 사용합니다.
공통 envelope와 워크플로우 문서 형태는 기존 모델을 따릅니다. 필드와 action 매핑은 API 담당자 검토 대상입니다.

### 파일 구조와 전송 방법

각 파일은 다음 항목을 묶은 문서용 컨테이너입니다. 파일 전체를 API에 제출하지 않습니다.

| JSON 위치 | 의미 | 사용 방법 |
| --- | --- | --- |
| `rest_response.body` | GET Task의 전체 waiting_input snapshot | 화면 전체 상태 복원 |
| `rest_response.body.interactions[0]` | 해당 HITL의 전체 공개 interaction | 표시 자료·허용 행동·입력 schema 읽기 |
| `sse_response.data` | SSE의 전체 JSON envelope | SSE 프레임의 data 문자열에 해당 |
| `sse_response.data.data` | interaction.opened의 전체 interaction | REST interactions[0]과 동일 |
| `resume_requests.<예시 이름>.body` | 해당 행동의 전체 resume request | POST /api/v1/workflow_run의 body |
| `resume_requests.<예시 이름>.headers` | 사용자·멱등 키·Content-Type 예시 | 실제 사용자와 제출별 키로 교체 |
| `resume_accepted_response.body` | resume 수락 시 전체 202 Task snapshot 예시 | 수락 확인. 최종 분석 결과가 아님 |

같은 파일의 여러 resume 예시는 **서로 다른 선택지**입니다. 같은 interaction에 순서대로 전부 제출하지 않습니다.
수락 후에는 같은 Task에 새 Run이 생성되며, 기존 interaction에 다른 요청을 다시 보내면 오래된 요청입니다.
네 파일의 UUID와 시각은 예시 값입니다. 실제 제출에는 수신한 resume_target을 복사합니다.
새 제출에는 새 Idempotency-Key, 통신 재시도에는 동일 키와 동일 body를 사용합니다.

SSE 프레임은 다음 구조이며, JSON 파일의 sse_response.data를 JSON 문자열로 넣습니다.

```text
id: 10
event: interaction.opened
data: <sse_response.data를 직렬화한 JSON 문자열>
```

rest_response의 last_event_sequence는 interaction.opened보다 클 수 있습니다.
예시에서는 interaction.opened 뒤 task.waiting_input이 저장된 상태를 GET으로 조회했습니다.
resume_accepted_response는 워커가 아직 시작하지 않은 pending 시점을 예시로 택했습니다.
실제 202 snapshot은 워커 진행에 따라 이미 running 등으로 변했을 수 있습니다.

### 1-A. 추천 워크플로우 선택

전체 JSON: [01_recommended_selection.json](examples/workflow_hitl_v2/01_recommended_selection.json)

- 최초 응답: 추천 후보 3개 각각의 전체 workflow와 출처·설명·이유·유사도·저장 버전, 현재 허용 행동 및 recommendation_search를 포함합니다.
- 같은 JSON의 `rerecommendation_examples.additional_matches`는 기존 3개 + 신규 1개를, `no_additional_matches`는 4개 유지와 안내 메시지를 보여줍니다. 각 예시의 `interaction`은 REST `interactions[]` 또는 SSE `data.data`에 들어가는 객체이며, 문서용 묶음 키를 API 필드로 구현하지 않습니다.
- 신규 1개는 threshold 이상 후보가 1개만 남은 경우입니다. 재추천마다 최대 3개를 추가하며 실제 결과 수만 반환합니다.
- 후보 이름 name은 선택적인 표시 필드입니다. 선택은 candidate_id로 합니다.
  후보의 표시용 name과 기존 워크플로우 문서 내부의 workflow.name은 별도 필드입니다.
- 첫 후보는 두 단계의 컬럼 분석, 두 번째는 iqr 고정 분석, 세 번째는 추가 분석 호출을 가진 설명용 후보입니다.
- 현재 화면에 제공한 candidate_id만 선택할 수 있도록 input_schema의 enum을 생성합니다.

| 요청 예시 이름 | 행동 | 제출 payload | 후속 의미 |
| --- | --- | --- | --- |
| `select` | 첫 후보 선택 | `{"intent":"select","candidate_id":"recommended_1"}` | 선택된 workflow의 입력 화면으로 진행 |
| `recommend_again` | 다시 추천 | `{"intent":"recommend_again"}` | 기존 후보를 유지하면서 추가 추천 요청. 없으면 안내와 기존 목록 유지 |
| `generate` | 워크플로우 생성하기 | `{"intent":"generate"}` | 후보 선택 없이 생성 경로로 전환 |
| `reject_new_workflow` | 후보 전체 거절 | `{"intent":"reject","reason":"이 후보 대신 새 워크플로우를 만들어 주세요."}` | 에이전트가 새 생성 요청 의도로 판단할 수 있음 |
| `reject_stop_analysis` | 후보 전체 거절 | `{"intent":"reject","reason":"분석 자체를 진행하지 않겠습니다."}` | 에이전트가 분석 중단 의도로 판단할 수 있음 |

예시 이름의 new_workflow/stop_analysis는 문서상 구분일 뿐 API 필드가 아닙니다.
서버가 받는 동작은 두 거절 모두 reject와 reason이며 특정 문장에 대한 하드코딩 규칙을 뜻하지 않습니다.
reason은 공백이 아닌 필수 문자열입니다. 재추천 feedback은 향후 수집용 선택 필드이며 현재 요청에서는 생략합니다.

### 1-B. 생성 워크플로우 선택

전체 JSON: [01_generated_selection.json](examples/workflow_hitl_v2/01_generated_selection.json)

추천 예시와 같은 kind 및 표시 구조입니다. 생성 개수는 `WORKFLOW_GENERATION_LIMIT`를 따르며 기본은 2개입니다.
연결된 JSON은 `WORKFLOW_GENERATION_LIMIT=1`인 경우의 단일 후보 예시이며, 프론트는 여러 생성 후보도 같은 배열 구조로 처리합니다.
기본 경로의 추가 단계가 아닌 대체 분기 예시입니다.

| 요청 예시 이름 | 행동 | 제출 payload |
| --- | --- | --- |
| `select` | 생성 후보 선택 | `{"intent":"select","candidate_id":"generated_1"}` |
| `regenerate` | 다시 생성 | `{"intent":"regenerate"}` |
| `reject_new_workflow` | 후보 거절 | `{"intent":"reject","reason":"이 후보 대신 새 워크플로우를 만들어 주세요."}` |
| `reject_stop_analysis` | 후보 거절 | `{"intent":"reject","reason":"분석 자체를 진행하지 않겠습니다."}` |

regenerate는 기존 후보 수정이 아니라 기존 생성 후보 전체를 제외한 새 후보 묶음 생성입니다.
feedback은 현재 수집하지 않습니다. 거절 대상은 이 interaction의 생성 후보 전체입니다.
위 추천 응답에는 regenerate, 생성 응답에는 recommend_again을 넣지 않았습니다.
이는 예시의 허용 행동 조합이며, 추후 정책 변경 시 서버가 제공하는 available_actions와 schema를 함께 변경합니다.
재추천·재생성에서 후보 제외 이력을 관리하는 것은 서버 책임이며 resume 요청에 제외 목록을 넣지 않습니다.

### 2. Tool input 입력

출력 연결 편집 규격을 반영한 전체 JSON: [02_tool_inputs.json](examples/workflow_hitl_v2/02_tool_inputs.json)

추천 예시에서 recommended_1을 선택한 다음 단계입니다. workflow_id는 workflow_1이며,
선택한 후보의 workflow와 동일한 문서를 전달합니다. 설명용 도구의 전체 인자는 다음과 같습니다.

```python
demo_load(dataset: str)
demo_analyze(data: object, column: str, method: str = "iqr", threshold: float | None = None)
```

| step/tool | 인자 | 표시 초기값 | 입력 가능 여부 |
| --- | --- | --- | --- |
| 0 / 0 | dataset | 미입력, has_value=false | 필수 입력 |
| 1 / 0 | data | demo_load의 data 출력 연결 | 수정 가능, 직접 값 또는 출력 연결 |
| 1 / 0 | column | 사용자 발화의 yield | 수정 가능, 필수 |
| 1 / 0 | method | 사용자 발화의 zscore, 도구 기본값은 iqr | 수정 가능, 선택 |
| 1 / 0 | threshold | 기본값 null, has_value=true | 수정 가능, 선택 |

표시 자료에는 다섯 인자가 모두 있으며 모두 editable=true입니다.
설명용 demo_load는 data(전체 데이터)와 selected_data(선별 데이터)를 반환한다고 가정합니다.
화면 공통 output_catalog에 demo_load의 두 반환값과 demo_analyze의 설명용 result를 한 번씩 제공합니다.
프론트는 첫 Tool에는 선행 출처를 표시하지 않고, demo_analyze에는 앞선 demo_load의 반환값만 표시합니다.
실제 반환값 목록은 Registry에서 얻으며 타입 호환성을 자동 보장하는 목록은 아닙니다.

제출 schema는 다섯 인자의 직접 값 및 제공된 출력 연결을 검증합니다.
기본값이 있는 method/threshold에는 source=default도 허용합니다.
중복 주소는 금지하며 binding이 없는 필수 dataset/column은 반드시 제출합니다.
기존 data 연결은 생략하면 유지하지만 새 화면에서는 선택한 연결을 명시적으로 제출합니다.

| 요청 예시 이름 | 사용자 행동 | 제출 내용 |
| --- | --- | --- |
| `submit_all` | 표시한 값·연결 유지 | 값 네 항목과 data 출력 연결 명시 |
| `clear_optional_to_defaults` | 선택 인자 기본값 복원 | method/threshold를 source=default로 제출 |
| `change_output_reference` | 기존 출력 연결 변경 | data의 reference.output을 selected_data로 변경 |
| `replace_output_with_value` | 기존 출력 연결을 직접 값으로 변경 | data를 source=value 및 예시 객체로 제출 |
| `legacy_values_only` | 기존 클라이언트 형식 | source 없는 값 네 항목; 생략한 data 연결 유지 |

null은 실제 값입니다. threshold의 null은 허용하지만 method의 null은 enum 위반입니다.
기본값 복원 요청에서는 method=iqr, threshold=null을 적용합니다.
03_plan_review.json은 change_output_reference 경로를 기준으로 작성했습니다.
각 resume 요청은 서로 대체하는 선택지이며 순서대로 모두 제출하는 것이 아닙니다.

### 3. 계획 승인

전체 JSON: [03_plan_review.json](examples/workflow_hitl_v2/03_plan_review.json)

Tool input의 change_output_reference 결과를 반영한 계획입니다. demo_analyze.data의
출처가 demo_load.data에서 demo_load.selected_data로 변경됩니다. execution_inputs에는
dataset_1, yield, zscore, null이 확정되어 있으며 출력 참조는 값 목록에 넣지 않습니다.
이 설명용 예시는 연결 구조를 보여주는 Workflow 1.0 정의를 사용합니다. 실제 서비스의 승인 화면에는 컴파일된 실행 워크플로우가 전달됩니다.
workflow와 execution_inputs를 함께 읽기 전용으로 보여주고 이 내용 그대로 승인받습니다.

| 요청 예시 이름 | 전송 action | payload | 처리 |
| --- | --- | --- | --- |
| `approve` | approve | 없음 | interaction에 저장된 계획·확정 입력 그대로 실행 |
| `reject_new_workflow` | edit | intent=reject, 필수 reason | 에이전트가 이유를 해석해 다음 경로 판단 |
| `reject_stop_analysis` | edit | intent=reject, 필수 reason | 에이전트가 분석 중단 의도인지 판단 |

승인 시 원본 workflow나 입력값을 다시 제출하지 않습니다. 승인 대상은 resume_target으로 식별한 서버의 불변 계획입니다.
approve의 actions[].input_schema는 빈 객체 형태이며, 이는 payload={}을 제출하라는 뜻이 아닙니다.
approve 요청에는 payload 키 자체가 없습니다. 기존 API와 동일하게 payload가 있으면 거부합니다.
계획 검토의 payload.input_schema는 edit, 즉 reason을 받는 거절 payload의 스키마입니다.

### 전송 action과 화면 행동의 대응

available_actions는 화면의 업무상 행동, actions는 API 전송 동작입니다. 같은 이름이라고 가정하지 않습니다.

| kind | available_actions.intent | command.resume.action | command.resume.payload |
| --- | --- | --- | --- |
| workflow_candidate_selection | select / recommend_again / generate / regenerate / reject | edit | 해당 intent의 객체 |
| additional_information | submit_inputs | edit | workflow ID/버전과 호출별 inputs |
| plan_review | approve | approve | 키 생략 |
| plan_review | reject | edit | intent와 reason |

이 매핑은 현재 세 kind의 v2 계약으로 고정해 제안합니다. label은 UI 표시용이므로 동작 판정에 사용하지 않습니다.
재추천 feedback은 schema상 선택 필드지만 현재 수집하지 않는 정책입니다. 단순 schema 자동 렌더러도 이를
현재 단계의 필수 질문으로 표시하면 안 됩니다. 피드백 입력 UI 도입은 별도 결정입니다.

### 검증 및 적용 범위

- 예시 전체의 REST/SSE 공통 envelope와 resume request는 기존 모델의 스키마 형태에 맞춰 검증했습니다.
- REST의 공개 interaction과 SSE의 interaction이 동일한지, edit action 스키마와 payload.input_schema가 같은지 확인했습니다.
- 각 정상 제출값은 해당 interaction의 JSON Schema에 통과하도록 작성했습니다.
- 없는 후보, 허용하지 않은 intent, 공백 reason, 중복 입력 주소, 필수값 누락, 읽기 전용 인자 제출,
  잘못된 입력 타입과 버전이 예시 스키마에서 거부되는지 확인했습니다.
- 버전 불일치는 실제 API에서 스키마 일반 오류로 처리하기 전에 stale 검사하여 기존 계약의 409로 매핑하는 것을 제안합니다.
- RunResource의 interrupt에는 설명용 내부 포맷도 포함되어 있습니다. payload_version을 내부에 보존하는 위치는 제안이며,
  프론트는 이 내부 필드를 해석하지 않고 공개 interactions만 사용합니다.
- demo 도구는 등록/실행하지 않았습니다. schema 검증은 실제 도구 컴파일·LLM 실행·DB 재개 통합 검증을 뜻하지 않습니다.
- 다음 단계는 실제 모델과 프론트를 포함한 전체 연동을 확인하는 것입니다.


## Tool 출력 연결 편집

공통 REST/SSE envelope, payload_version=2, action_name=additional_information,
intent=submit_inputs는 유지합니다. 새 표시 데이터의 모든 명시적 Tool 인자는
editable=true입니다. 기존 에이전트 출력 연결도 수정할 수 있습니다.

payload.arguments.output_catalog에 화면 전체의 반환값 정보를 한 번만 전달합니다.
각 파라미터에는 output_options를 반복하지 않으며 현재 binding만 둡니다.
카탈로그는 step_index/skill → tools[tool_index/tool] → outputs[name/type/description/selectable] 구조입니다.
인덱스는 0부터 시작하며 tool_index는 해당 step 안에서의 위치입니다.
같은 스킬/Tool의 반복 호출도 인덱스로 구분합니다. 반환값은 실제 실행 결과가 아니라 Registry 정의입니다.

```json
{
  "output_catalog": [
    {
      "step_index": 0,
      "skill": "data_load",
      "tools": [
        {
          "tool_index": 0,
          "tool": "extract_data",
          "outputs": [
            {"name": "data", "type": null, "description": "", "selectable": true}
          ]
        }
      ]
    }
  ]
}
```

위는 공통 목록 부분만 발췌한 예시입니다. 같은 arguments에 workflow, tool_inputs,
available_actions도 함께 전달합니다. 카탈로그에는 현재/후속 Tool 정보도 포함되므로
프론트는 각 입력 대상에 대해 다음 조건으로 목록을 걸러야 합니다.

- 출처 step_index < 대상 step_index, 또는 같은 step에서 출처 tool_index < 대상 tool_index.
- 출력의 selectable=true.

selectable은 실행 누락 가능성을 고려한 출처 가용성입니다. 조건부 Tool의 출력은
컴파일러가 미실행 시에도 전달 가능하다고 취급하는 passthrough 출력만 true입니다.
반환 타입 메타데이터가 없으면 type=null입니다. selectable은 입력 타입 호환성을 보장하지 않습니다.

프론트는 입력 방식에 직접 값/기본값/선행 Tool 출력을 제공합니다.
기본값 방식은 has_default=true일 때만 제공합니다.
선행 Tool 출력 방식을 선택하면 세 드롭다운을 활성화합니다:

1. 스킬: step_index — skill
2. Tool: 해당 스킬의 tool_index — tool
3. 반환값: 해당 호출의 outputs[].name (있으면 타입·설명도 표시)

선행 순서와 selectable 조건으로 걸러낸 결과가 없으면 출력 방식은 제공하지 않습니다.
기존 binding이 있으면 출력 방식을 선택한 상태에서 해당 세 값을 미리 선택합니다.
`demo_v2.html`은 이 연동 드롭다운을 구현합니다. 다른 화면은 같은 데이터로 UI를 구현해야 합니다.

inputs 항목은 다음 세 형식 중 하나입니다. 아래 예시는 서로 대체하는 형식이며
동일 주소를 중복 제출하면 안 됩니다.

```json
{"step_index": 1, "tool_index": 0, "parameter": "data", "source": "value", "value": {"sample": [1]}}
```

```json
{"step_index": 1, "tool_index": 0, "parameter": "data", "source": "tool_output", "reference": {"step_index": 0, "tool_index": 1, "output": "data"}}
```

```json
{"step_index": 1, "tool_index": 1, "parameter": "columns", "source": "default"}
```

value는 실제 값이며 null과 빈 문자열도 해당 타입이 허용할 때 그대로 전달합니다.
reference는 출처만 나타내며 실제 반환값은 실행 시 결정됩니다.
기본값 제출에는 value/reference를 포함하지 않습니다.
기존 source 없는 value 제출은 직접 값으로 받습니다.
기존 클라이언트 호환을 위해 기존 binding이 있는 인자를 생략하면 연결을 유지합니다.
기존 연결을 기본값으로 바꾸려면 반드시 source=default를 명시합니다.
binding이 없는 선택 인자를 생략하면 기존처럼 기본값을 적용하고,
binding이 없는 필수 인자를 생략하면 거부합니다.

제출 reference의 output에는 선택한 outputs[].name을 넣습니다. resume 형식은 변경하지 않습니다.
제출용 JSON Schema는 공통 ToolOutputReference 정의를 $ref로 재사용하고 후보 enum을 반복하지 않습니다.
따라서 출처의 존재 여부·선행 순서·selectable은 JSON Schema만으로 검증하지 않습니다.
API와 에이전트가 저장된 output_catalog에 대해 별도로 검증합니다.
기존에 저장된 parameter.output_options 형식의 pending interaction도 호환 처리합니다.
output_catalog가 있으면 그것을 기준으로 검증하며, legacy 목록으로 우회하지 않습니다.
에이전트는 실행용 workflow_input_definition에 연결을 적용하고 컴파일합니다.
직접 값/기본값은 execution_inputs에, 출력 연결은 워크플로우 arguments의
source=tool_output에 반영됩니다. 원본 카탈로그 정의는 변경하지 않습니다.
계획 승인 화면은 이 연결까지 반영한 완성된 워크플로우를 보여줍니다.

기존 pending 화면이 editable=false인 인자는 계속 수정할 수 없습니다.
새 표시 데이터로 생성된 입력 단계부터 출력 연결 편집을 사용합니다.
LangGraph Agent Chat 및 CLI에서는 `@output {"step_index":0,"tool_index":1,"output":"data"}`로
연결을 입력하고 `:default`로 기본값을 명시할 수 있습니다.
