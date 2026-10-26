# API 서비스 통합 및 Chat Completion/HITL 계약안

## 1. 목적과 현재 상태

차주 API 서비스 통합 시 일반 사용자 메시지와 HITL 응답을 하나의 Chat Completion
endpoint로 처리하기 위한 권장 계약이다.

현재 저장소에는 `POST /{workflow}/api/completion` 초안이 있지만, 외부 API 서비스와
통합할 때는 요청 종류 판별, checkpoint 소유권 검증, HITL resume, streaming 이벤트
계약을 보강해야 한다. 이 문서는 구현된 LangGraph HITL envelope를 기준으로 한
통합안이다.

## 2. 핵심 원칙

- `session_id`를 LangGraph `thread_id`로 일관되게 사용한다.
- 일반 메시지는 새 graph input으로 전달한다.
- HITL 응답은 새 사용자 메시지가 아니라 `Command(resume=...)`로 전달한다.
- `interrupt_id`를 함께 받아 현재 대기 중인 interrupt와 일치하는지 확인한다.
- 사용자에게 보여주는 HITL과 내부 `EXECUTOR_EVENT` interrupt를 구분한다.
- `user_id`, `project_id`, `session_id` 소유권을 resume 전에 검증한다.
- 동일 HITL 응답 재전송을 멱등하게 처리한다.

## 3. 하나의 endpoint와 요청 구분

```http
POST /{workflow}/api/completion
Content-Type: application/json
```

권장 Pydantic 구조는 discriminator를 가진 두 요청 형식이다.

```python
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class MessageCompletionRequest(BaseModel):
    input_type: Literal["message"]
    user_id: str
    project_id: str
    session_id: str
    message: str
    stream: bool = False


class HitlResume(BaseModel):
    interrupt_id: str
    response: dict


class HitlCompletionRequest(BaseModel):
    input_type: Literal["hitl_response"]
    user_id: str
    project_id: str
    session_id: str
    resume: HitlResume
    stream: bool = False


CompletionRequest = Annotated[
    Union[MessageCompletionRequest, HitlCompletionRequest],
    Field(discriminator="input_type"),
]
```

## 4. 일반 메시지 처리

요청 예시:

```json
{
  "input_type": "message",
  "user_id": "user-001",
  "project_id": "project-001",
  "session_id": "session-001",
  "message": "불량 원인 분석을 해줘",
  "stream": false
}
```

Graph 호출:

```python
config = {"configurable": {"thread_id": request.session_id}}
result = await graph.ainvoke(
    {
        "user_id": request.user_id,
        "project_id": request.project_id,
        "session_id": request.session_id,
        "thread_id": request.session_id,
        "user_request": request.message,
    },
    config=config,
)
```

Graph가 끝나면 일반 assistant 응답을 반환하고, 사용자 입력이 필요하면
`__interrupt__`를 HITL 응답으로 변환한다.

## 5. HITL 요청 응답

Graph의 사용자용 interrupt value는 다음 표준 envelope를 사용한다.

```json
{
  "action_requests": [
    {
      "name": "data_selection",
      "args": {
        "data_count": 2,
        "datasets": []
      },
      "description": "분석 데이터를 확인해주세요."
    }
  ],
  "review_configs": [
    {
      "action_name": "data_selection",
      "allowed_decisions": ["approve", "edit"]
    }
  ]
}
```

API 응답에서는 interrupt ID를 반드시 같이 내려준다.

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "choices": [],
  "status": "requires_action",
  "hitl": {
    "interrupt_id": "interrupt-...",
    "request": {
      "action_requests": [],
      "review_configs": []
    }
  }
}
```

`status`와 `hitl`은 OpenAI-compatible 기본 필드 밖의 서비스 확장 필드로 정의한다.
API 서비스와 UI가 함께 이 계약을 사용해야 한다.

## 6. HITL 응답과 resume

승인 요청 예시:

```json
{
  "input_type": "hitl_response",
  "user_id": "user-001",
  "project_id": "project-001",
  "session_id": "session-001",
  "resume": {
    "interrupt_id": "interrupt-...",
    "response": {
      "decisions": [
        {"type": "approve"}
      ]
    }
  }
}
```

수정 승인:

```json
{
  "input_type": "hitl_response",
  "user_id": "user-001",
  "project_id": "project-001",
  "session_id": "session-001",
  "resume": {
    "interrupt_id": "interrupt-...",
    "response": {
      "decisions": [
        {
          "type": "edit",
          "edited_action": {
            "name": "data_selection",
            "args": {
              "data_count": 2,
              "datasets": []
            }
          }
        }
      ]
    }
  }
}
```

서버는 동일 thread의 현재 state를 먼저 읽고 interrupt ID와 소유권을 검증한 후
다음처럼 재개한다.

```python
config = {"configurable": {"thread_id": request.session_id}}
snapshot = await graph.aget_state(config)

# 현재 대기 interrupt, interrupt_id, user/project/session 소유권 검증

result = await graph.ainvoke(
    Command(
        resume={
            request.resume.interrupt_id: request.resume.response,
        }
    ),
    config=config,
)
```

현재 Graph는 interrupt가 하나일 때 다음 단순 형식도 처리할 수 있다.

```python
Command(resume=request.resume.response)
```

그러나 API 통합에서는 오래된 화면이나 중복 탭이 다른 interrupt에 답하는 것을 막기
위해 `interrupt_id → response` 매핑 방식을 권장한다.

## 7. Decision 형식

| type | 필수 값 | 의미 |
|---|---|---|
| `approve` | 없음 | interrupt 기본 args 승인 |
| `edit` | `edited_action.args` | 사용자가 수정한 args로 진행 |
| `reject` | 선택 `message` | 후보/Workflow 거절과 피드백 |
| `respond` | `message` | 질문 답변. JSON 문자열이면 도메인 객체로 파싱 가능 |

데이터 선택 approve 시 Graph의 `unwrap_hitl_response()`가 interrupt 기본값을 사용한다.
edit 시에는 `edited_action.args`가 실제 `Command(resume=...)`의 도메인 값이 된다.

## 8. 사용자 HITL과 Executor interrupt 구분

Executor 대기 interrupt는 다음 형태다.

```json
{
  "kind": "EXECUTOR_EVENT",
  "task_id": "...",
  "execution_id": "..."
}
```

이는 UI가 답변할 HITL이 아니다. Redis Worker가
`execution.operation_completed` 또는 `execution.completed` 이벤트를 받아 자동으로
resume한다. API 서비스는 이 interrupt를 사용자에게 Edit/Approve 카드로 노출하지
않아야 한다.

사용자용 HITL은 `action_requests`와 `review_configs` envelope를 가진다.

## 9. Streaming

`stream=true`에서는 일반 assistant token을 OpenAI-compatible SSE chunk로 전달한다.
사용자 입력이 필요해지면 서비스 전용 이벤트를 보낸 뒤 현재 HTTP stream을 끝낸다.

```text
event: hitl_required
data: {"interrupt_id":"...","request":{...}}

data: [DONE]
```

사용자가 응답하면 같은 completion endpoint에 `input_type=hitl_response`로 새 HTTP
요청을 보낸다. 이전 stream 연결에 응답을 쓰는 방식이 아니다.

Executor 이벤트로 Graph가 백그라운드에서 재개되는 구간은 최초 HTTP 요청 수명보다
길 수 있다. 최종 리포트를 UI에 전달하려면 API 서비스가 다음 중 하나를 추가로
정해야 한다.

- session/task event SSE 또는 WebSocket 구독
- 동일 completion endpoint의 상태 조회 동작
- 별도 task 상태 조회 endpoint

하나의 POST 요청을 실행 완료까지 계속 열어두는 방식만 사용하면 네트워크 timeout과
Worker의 별도 resume 실행 때문에 최종 리포트 전달이 불안정할 수 있다.

## 10. 오류 및 동시성 처리

권장 HTTP 처리:

| 상황 | 권장 응답 |
|---|---|
| 존재하지 않는 workflow | `404` |
| 다른 사용자/프로젝트의 session | `403` |
| 대기 interrupt 없음 | `409` |
| interrupt ID 불일치 또는 이미 처리됨 | `409` |
| HITL schema 오류 | `422` |
| 동일 session에 실행 중 요청 존재 | `409` 또는 멱등 응답 |
| 내부 Graph/Executor 오류 | `500` 또는 task 실패 상태 |

`request_id` 또는 별도 idempotency key를 받아 동일 요청 재전송이 새 Graph 실행을
중복 생성하지 않도록 하는 것이 좋다.

## 11. 통합 체크리스트

- API 서비스와 Agent가 동일한 session/thread 식별 규칙 사용
- 사용자·프로젝트·session 소유권 저장 및 검증
- 일반 message와 HITL response discriminator 적용
- HITL 응답은 graph input이 아닌 `Command(resume=...)` 사용
- interrupt ID 검증
- 사용자 HITL과 `EXECUTOR_EVENT` 분리
- non-stream/stream 응답 계약 합의
- 백그라운드 Worker resume 이후 최종 리포트 전달 채널 결정
- 중복 요청 및 오래된 HITL 응답의 `409` 처리
- 로그에 사용자 데이터, DB URL, credential 기록 금지

데이터 선택 상세 구조는
[data_selection_api_contract.md](./data_selection_api_contract.md)를 참고한다.

