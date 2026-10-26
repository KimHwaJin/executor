# Workflow HITL v2 에이전트·API 연동 정리

## 1. 규격 검토 결과

공유한 JSON의 필드·타입·action은 현재 형태로 사용할 수 있습니다. 예시 파일에 추천→생성 전환 요청을 추가했습니다.
API의 버전 보존·제출 검증·resume 변환은 반영했습니다.
에이전트는 공통 헬퍼·표시 모델과 후보 선택 → Tool input → 계획 승인 v2 경로를 연결했습니다.
추천 검색은 미구현이며 현재 생성 후보 흐름으로 검증합니다. 데모 적용 결과는 [브라우저 검증](demo_v2_validation.md)을 참고하세요.

실제 연동 시 다음을 지킵니다.

- 예시 UUID 대신 실제 응답의 `resume_target`을 사용합니다.
- `payload.arguments`는 화면 표시 자료이고, `payload.input_schema`는 **제출 payload만** 검증하는 스키마입니다.
- 후보 선택·재추천·재생성·후보 거절·Tool input 제출은 `action="edit"`입니다.
- 계획 승인은 `action="approve"`이며 payload를 보내지 않습니다.
- 계획 거절도 `action="edit"` + `{"intent":"reject","reason":"..."}`입니다.
- v2에서는 바깥 action에 `reject`, `select`, `workflow_approval` 같은 별칭을 보내지 않습니다.
- 클라이언트가 resume에 `payload_version`을 추가하지 않습니다. API는 저장된 interrupt의 버전을 사용합니다.

API 검증 통과는 실제 도구 실행 가능성까지 검증했다는 의미는 아닙니다. 예시의 demo 도구와 에이전트 후속 처리는 별도 연결이 필요합니다.

## 2. 에이전트 → API HITL 연결 형식

### HITL 발행

현재 함수는 `app.graphs.hitl_protocol`의 `build_hitl_request()`, `request_human_input()`, `unwrap_hitl_response()`입니다.

두 생성 함수에 `payload_version` 인자를 추가했습니다. 기본값 1은 기존 interrupt 형식을 유지하며
버전 필드를 생략하고, 2를 지정하면 최상위에 `payload_version=2`를 붙입니다.
아래 `display_arguments`와 `submission_schema`는 에이전트가 해당 단계에 맞게 생성한 객체입니다.

```python
from langgraph.types import interrupt
from app.graphs.hitl_protocol import (
    build_hitl_request,
    unwrap_hitl_response,
)

request = build_hitl_request(
    action_name="workflow_candidate_selection",
    description="워크플로우를 선택해 주세요.",
    args=display_arguments,
    allowed_decisions=["edit"],
    args_schema=submission_schema,
    payload_version=2,
)

raw_response = interrupt(request)
answer = unwrap_hitl_response(
    raw_response,
    default_args=request["action_requests"][0]["args"],
)
```

`request_human_input(..., payload_version=2)`도 같은 버전 처리를 지원합니다.
세 단계의 새 요청은 v2를 사용하며, 버전 상태가 없는 기존 checkpoint는 v1 호출과 해석을 유지합니다.

| 단계 | action_name | allowed_decisions |
| --- | --- | --- |
| 후보 선택 | `workflow_candidate_selection` | `["edit"]` |
| Tool input | `additional_information` | `["edit"]` |
| 계획 승인·거절 | `workflow_approval` | `["approve", "edit"]` |

### Resume 원본과 행동별 값

API가 resume를 검증한 뒤 그래프에 전달하는 원본은 다음 형태입니다.

```json
{
  "decisions": [
    {
      "type": "edit",
      "edited_action": {
        "name": "workflow_candidate_selection",
        "args": {
          "intent": "select",
          "candidate_id": "recommended_1"
        }
      }
    }
  ]
}
```

이 객체가 `Command(resume=...)`로 전달됩니다. `edited_action.name`은 해당 단계의 action_name이며, `unwrap_hitl_response()`를 거치면 `edited_action.args`만 반환됩니다.

행동별 **unwrap 이후 값**은 다음과 같습니다.

후보 선택:

```json
{"intent":"select","candidate_id":"recommended_1"}
```

재추천:

```json
{"intent":"recommend_again"}
```

재생성:

```json
{"intent":"regenerate"}
```

재추천·재생성에는 선택 문자열 `feedback`을 추가할 수 있으며 생략 가능합니다.

후보 거절 또는 계획 거절:

```json
{"intent":"reject","reason":"다른 워크플로우로 분석하고 싶습니다."}
```

Tool input 제출 형식 예시입니다. 실제로는 해당 interaction의 모든 필수 editable 입력을 포함해야 합니다.

```json
{
  "intent": "submit_inputs",
  "workflow_id": "workflow_1",
  "workflow_revision": 1,
  "inputs": [
    {
      "step_index": 0,
      "tool_index": 0,
      "parameter": "dataset",
      "value": "dataset_1"
    }
  ]
}
```

계획 승인의 원본은 다음과 같습니다.

```json
{"decisions":[{"type":"approve"}]}
```

**approve를 unwrap하면 `{"approved":true}`가 아니라 `default_args`가 반환됩니다.** 승인·거절을 명시적으로 구분하려면 계획 검토 노드에서 원본 decision의 `type`을 먼저 확인하는 편이 명확합니다.

```python
raw = interrupt(request)
decision = raw["decisions"][0]

if decision["type"] == "approve":
    # 해당 interaction에서 보여준 계획과 execution_inputs를 사용
    approved_plan = request["action_requests"][0]["args"]
else:
    answer = unwrap_hitl_response(raw)
    # API 검증을 거친 {"intent": "reject", "reason": "..."}
    reason = answer["reason"]
```

위 분기는 v2 계획 검토용입니다. 현재 plan_review_v2.py에서 원본 decision을 검사하도록 연결했으며, 기존 v1 checkpoint 처리도 유지합니다. 두 거절 모두 API가 Task 취소로 바꾸지 않고 에이전트에 전달합니다.

## 3. 메시지 전달 형식

노드에서는 기존처럼 `messages` 상태 업데이트를 반환합니다. 현재 기본 메시지 저장 경로에서는 다음처럼 구분됩니다.

| 그래프 메시지 role | SSE data.role | SSE data.channel |
| --- | --- | --- |
| `agent` | `assistant` | `commentary` |
| `assistant` | `assistant` | `final` |

진행 안내 노드 예시:

```python
return {
    "messages": [
        {
            "role": "agent",
            "name": "workflow_planner",
            "content": "입력값을 확인하고 실행 계획을 준비하고 있습니다.",
        }
    ]
}
```

최종 답변 노드 예시:

```python
return {
    "messages": [
        {
            "role": "assistant",
            "name": "analysis_result",
            "content": {"answer": "분석을 완료했습니다. 결과는 다음과 같습니다."},
        }
    ]
}
```

`content`는 문자열을 사용하거나, 객체의 `message`·`answer`·`final_response`에 사용자용 문장을 넣습니다.

기존 persistence dispatcher가 `app.services.chat_crud_message_sink.save_graph_message()`를 호출해 메시지와 `message.completed` 이벤트를 저장합니다. 노드에서 별도로 DB 저장 함수를 호출할 필요는 없습니다. 기존 실행 경로는 현재 실행 Run의 `agent_run_id`를 저장 함수에 전달하며, 이 연결을 통해 메시지와 Task 이벤트가 함께 저장됩니다.

FE에는 `message.completed` 이벤트의 data로 다음 형태가 전달됩니다.

```json
{
  "message_id": "...",
  "role": "assistant",
  "channel": "commentary",
  "content": [
    {
      "type": "text",
      "text": "입력값을 확인하고 실행 계획을 준비하고 있습니다."
    }
  ]
}
```

현재는 메시지 객체에 `"channel":"commentary"`만 넣어서는 채널이 바뀌지 않습니다. 기본 경로의 role 매핑을 사용해야 합니다. 사용자 메시지도 final 채널로 전달되므로 FE는 data.role도 확인합니다.

같은 노드에서 interrupt 전에 준비만 하고 반환하지 않은 메시지는 전달이 보장되지 않습니다. 진행 안내가 먼저 보여야 한다면 앞선 노드의 상태 업데이트로 내보내야 합니다. final 메시지와 Task 종료는 별개이므로 FE의 완료 판단은 `task_finished`를 사용합니다.

## 4. 역할 분담과 버전 처리

### 공통 스키마 모델

공통 제출 모델은 `src/app/schemas/common/workflow_hitl_schema.py`에 추가했습니다.

- `CandidateSelection`
- `CandidateRetry`
- `CandidateGeneration`: 추천 후보 화면의 generate 제출 (추가 필드 없음)
- `ReasonRejection`
- `ToolInputValue`
- `ToolInputSubmission`
- `CANDIDATE_SUBMISSION`: 후보 행동 union 검증용 TypeAdapter

에이전트도 `app.schemas.common.workflow_hitl_schema`에서 import해서 사용합니다.
같은 모듈에 다음 표시 모델과 동적 제출 스키마 함수를 추가했습니다.

| 표시 모델 | 동적 제출 스키마 함수 |
| --- | --- |
| `CandidateSelectionDisplay` (`WorkflowCandidate`, `AvailableAction`) | `build_candidate_submission_schema()` |
| `ToolInputDisplay` (`ToolCallDisplay`, `ToolParameterDisplay`) | `build_tool_input_submission_schema()` |
| `PlanReviewDisplay` | `build_plan_review_submission_schema()` |

```python
from app.schemas.common.workflow_hitl_schema import (
    CandidateSelectionDisplay,
    build_candidate_submission_schema,
)

display = CandidateSelectionDisplay.model_validate(display_arguments)
request = build_hitl_request(
    action_name="workflow_candidate_selection",
    description="워크플로우를 선택해 주세요.",
    args=display.model_dump(mode="json"),
    allowed_decisions=["edit"],
    args_schema=build_candidate_submission_schema(display),
    payload_version=2,
)
```

모델은 표시 데이터의 형태와 중복 주소 등을 검증합니다. `model_dump(mode="json")`는 미입력 value를
생략하고 명시적 null/false/0은 보존합니다. 입력 스키마는 표시된 후보 ID·허용 intent·입력 주소·타입·필수값을 제한합니다.
도구별 value_schema는 자체로 검증 가능한 JSON Schema여야 합니다. 로컬 `$defs/$ref`도 지원합니다.
계획 검토 스키마는 edit/reason용이며 approve는 여전히 payload 없이 원본 decision으로 구분합니다.

도구 등록 정보에서 표시 자료 수집, 기본값 반영, Tool input/계획 승인 v2 전환은 연결했습니다.
단계별 구현 및 외부 서비스 검증 범위는 아래 구현 현황을 따릅니다.
표시 모델의 workflow 객체는 기존 도메인 정의를 보존하며 실제 워크플로우 컴파일을 수행하지 않습니다.

정적 제출 모델만으로는 현재 후보 ID, 입력 주소, 도구별 타입을 모두 제한할 수 없습니다. 에이전트는 실제 표시 자료를 기준으로 동적 `args_schema`와 `available_actions`를 일관되게 생성해야 합니다.

### 검증 및 처리 책임

| 구분 | API 담당 | 에이전트 담당 |
| --- | --- | --- |
| 제출 대상 | 사용자 소유권, 최신 Run, interaction ID/revision 검사 | 해당 checkpoint의 상태 복원 |
| 후보 선택 | 허용 intent, 저장된 후보 ID 검사 | 후보 생성, 선택 적용, 재추천·재생성 및 제외 이력 |
| Tool input | workflow ID/version, 주소, 중복, 필수값, editable, 타입·스키마 검사 | 도구 정의 기반 표시 자료 생성, 기본값 적용, 컴파일·실행 가능성 검사 |
| 계획 | approve/edit 허용 및 거절 사유 검사 | 표시한 계획·입력 그대로 승인 처리, 거절 후 실행 방지 |
| 거절 | reason을 변경하지 않고 전달 | reason과 맥락을 해석해 재계획·확인·중단 결정 |
| 메시지 | 기존 저장·SSE 전달 경로 유지 | 처리 시점에 맞는 사용자 문장과 role 생성 |

API 검증 실패는 새 Run 생성 전에 422, 오래된 interaction/revision 및 유효한 정수인 workflow_revision 불일치는 409로 반환합니다. workflow_id 불일치, workflow_revision 누락·타입 오류·0 이하 값은 422입니다.

API는 Tool input의 생략된 선택 인자를 기본값으로 채우지 않습니다. 에이전트가 도구 정의에 따라 기본값으로 복원합니다. 0, false, null은 생략과 구분하여 그대로 전달합니다.

에이전트 실행 중 발견한 컴파일 오류는 이미 수락된 요청의 422로 되돌릴 수 없으므로 후속 interaction이나 실행 상태로 안내해야 합니다.

### payload_version 설정·저장·전달

1. 에이전트가 v2 생성·해석 준비가 된 노드에서 interrupt 최상위에 `payload_version=2`를 설정합니다.
2. 기존 실행 저장 경로가 interrupt dict를 그대로 Run에 저장합니다.
3. API projection이 저장된 버전을 REST/SSE interaction에 전달합니다.
4. API resume 검증은 클라이언트 값이 아니라 저장된 버전을 기준으로 분기합니다.
5. 버전이 없는 기존 입력 대기 건은 v1으로 유지합니다. 에이전트도 기존 checkpoint 재개 호환을 유지해야 합니다. API는 미지원 버전의 resume를 409로 거부합니다.

세 버전 필드의 의미는 구분합니다.

| 필드 | 의미 |
| --- | --- |
| `payload_version` | HITL payload 계약 버전 |
| `interaction.revision` | 입력 대기 항목의 revision |
| `workflow_revision` | 표시한 워크플로우 버전 |

## 5. 후보 선택 노드 v2 연결 현황

- 새 요청의 `announce_workflow_search`가 checkpoint state에 `candidate_selection_payload_version=2`를 기록합니다.
  버전 상태가 없는 기존 후보 선택 대기 checkpoint는 이전 v1 폼과 응답 해석을 그대로 사용합니다.
- `make_select_workflow_candidate()`는 생성/추천 내부 후보를 CandidateSelectionDisplay로 변환하고,
  동적 제출 스키마와 `allowed_decisions=["edit"]`, `payload_version=2`로 interrupt를 발행합니다.
- 선택은 candidate_id로 대상을 찾고 기존 컴파일·입력/승인 라우팅으로 이어집니다.
- 생성 후보는 하나이며 select/regenerate/reject를 제공합니다. 최대 workflow revision에 도달하면 regenerate는 제공하지 않습니다.
- regenerate는 기존 요청을 유지하고 현재 생성 후보를 제외 이력에 넣은 뒤 생성 경로로 돌아갑니다.
  생성기는 이력을 전달받고 기존 steps/execution_mode와 같은 결과를 거부합니다. 이름/actor만 바뀐 결과도 중복입니다.
  비교 기준은 실행 구조의 정확한 일치이며, 의미상 유사한 구조까지 판정하는 추천/유사도 기능은 아닙니다.
  검증 재시도 한도 내 새 후보를 만들지 못하면 기존 생성 실패 경로로 종료하고 같은 후보를 다시 제공하지 않습니다.
- reject는 reason을 approval_feedback과 거절 컨텍스트에 담아 기존 라우팅 에이전트로 전달합니다.
  새 생성 요청인지 분석 중단인지 라우터가 판단하며, 후보 선택 노드 자체가 거절을 취소로 단정하지 않습니다.
- 후보 준비·생성 메시지는 role=agent로 진행 안내에 전달하고 전체 workflow는 interaction 표시 데이터로 제공합니다.

추천 검색은 아직 구현/연결하지 않았습니다. 추천 후보 목록을 주입하면 같은 노드에서 표시·선택·거절이 가능하지만,
현재 production graph에서는 추천 및 recommend_again을 노출하지 않습니다.
표시 어댑터는 `can_recommend_again` 기능 인자를 지원하므로, 이후 추천 검색·제외 이력·라우팅을 연결한 뒤에만 활성화합니다.
추천 출처의 행동은 select/recommend_again/generate/reject, 생성 출처는 select/regenerate/reject로 분리하며 혼합 라운드는 현재 허용하지 않습니다.
generate는 구현된 생성 경로로 연결되어 있으며 추천 검색 자체의 구현과는 독립적입니다.
현재 추천 후보를 주입하면 select/generate/reject를 제공하며, 실제 추천 검색이 연결되기 전까지 recommend_again은 비활성입니다.
generate의 resume는 action=edit, payload={"intent":"generate"}이고, 이전 요청을 유지한 채 생성합니다.
추천 후보를 생성 제외 이력에 추가하지 않으며 기존 승인·거절 피드백을 초기화합니다.
생성 한도에 도달하면 generate/regenerate 모두 노출하지 않습니다. CLI도 generate 입력을 지원합니다.

로컬 CLI도 새 후보 ID/intent 제출을 지원합니다. 새 workflow-first 시나리오는 후보 선택·Tool input·계획 승인 모두 v2입니다. 프론트는 단계 이름이 아닌 실제 payload_version을 따라야 합니다.

검증은 추천 서버 없이 주입한 후보, 인메모리 LangGraph checkpoint, 실제 API projection/resume 변환과 mock 생성기를 사용했습니다.
추천 검색 품질이나 실제 LLM의 재생성 품질을 검증한 것은 아닙니다.

## 6. 행동 등록부

`src/app/schemas/common/workflow_hitl_actions.py`의 `ACTIONS`에서 행동을 관리합니다.
각 항목은 단계(scope), intent, 표시 문구(label), 제출 모델, 전송 action,
후보 출처(origins), 기능 활성 조건(capability), 처리 함수(handler), 이동 노드(destination)를 정의합니다.

- 후보의 `available_actions`, 동적 제출 스키마, 제출 모델 검증, 처리 분기와 CLI 필수값 입력이 이 등록부를 참조합니다.
- 표시 문구·순서·추천/생성 화면의 노출 조건을 바꿀 때는 등록부를 수정합니다. 기능 구현 여부와 생성 횟수 한도는 노드가 전달하는 capability로 제한합니다.
- 선택 대기 중인 기존 요청과의 호환을 위해 행동을 숨길 때는 등록 정의 자체를 삭제하기보다 노출 조건을 변경합니다.
- 새 행동은 제출 모델과 처리 함수를 만들고 등록합니다. 새로운 이동 노드가 필요하면 그래프 노드와 edge도 연결해야 합니다.
- 제출 모델은 `workflow_hitl_submissions.py`에 분리하며 기존 `workflow_hitl_schema.py` import 경로도 유지합니다. API 공통 envelope와 외부 JSON 형식은 변경하지 않습니다.
- Tool input은 등록부의 submit_inputs와 v2 노드를 연결했습니다. 계획 승인도 등록부의 approve/reject를 사용합니다.
- `recommend_again`은 규격 정의만 등록되어 있으며 실제 추천 처리·라우팅 연결 전에는 활성화하지 않습니다.

등록부 변경은 코드 변경 후 재시작/배포하는 방식입니다. 저장된 interrupt의 행동 목록을 실행 중에 덮어쓰지 않습니다.

## 7. Tool input 노드 v2 연결

새 v2 후보 선택에서 Workflow 1.0을 선택하면 `tool_input_payload_version=2`를 설정합니다.
`workflow_inputs_v2.py`는 Tool Registry의 모든 명시적 인자를 읽어 `ToolInputDisplay`와 제출 스키마를 만듭니다.
값이 이미 모두 채워졌어도 입력 확인 단계를 거칩니다. 가변 인자(*args/**kwargs)는 기존 컴파일러와 동일하게 개별 입력 대상에서 제외합니다.

- 필수 미입력은 `has_value=false`, 기본값은 `value_origin=default`, 사용자 발화 기반 값은 `user_request`로 표시합니다.
- 기존 워크플로우 literal은 `workflow_literal`로 구분하고 수정할 수 있습니다. 사용자 발화로 채웠다고 추정하지 않습니다.
- 이전 tool 출력 참조는 `editable=false`, `value_origin=tool_output`, `binding`으로 전달하며 제출은 거부합니다.
- 발행 action_name은 기존 `additional_information`, transport action은 `edit`, 제출 intent는 `submit_inputs`입니다.
- `workflow_id`, `workflow_revision`, 0부터 시작하는 step/tool 인덱스와 parameter로 제출 대상을 검증합니다.
- 필수 입력은 기존 표시값이 있더라도 모두 제출합니다. 선택 인자 생략은 기존 값 유지가 아닌 Registry 기본값 복원입니다.
  화면에서 선택 인자를 비웠다면 해당 입력 항목을 제출 목록에서 제외합니다. 명시적인 `null`은 nullable인 경우에만 실제 값으로 허용합니다.
- 주소 중복·읽기 전용·잘못된 타입·버전·누락은 승인으로 넘어가지 않습니다. API 오류 코드는 기존 409/422 규격을 유지합니다.
  API를 거치지 않는 직접 graph/CLI 제출도 검증하며, 오류 시 기존 입력을 보존하고 입력 화면을 다시 표시합니다.

재사용 원본 `workflow_definition`과 카탈로그는 보존합니다. 이번 실행용 `workflow_input_definition` 사본에서
수정 가능한 인자를 input binding으로 바꾸고, 확정된 `execution_inputs`를 결정적으로 컴파일합니다.
이 과정에서 LLM을 호출하거나 사용자가 제출한 값을 추가로 수정하지 않습니다.
완성된 compiled workflow는 v2 계획 승인 화면으로 넘깁니다. 입력 반영 메시지는 role=agent로 commentary에 전달합니다.

기존 입력 대기 checkpoint에 `tool_input_payload_version`이 없으면 종전 v1 입력 형식을 유지합니다.
CLI도 v2 전체 인자를 표시하고 제출하며, Enter는 표시값 사용, `:default`는 선택 인자 기본값 복원입니다.

검증 범위: 실제 Registry/컴파일러, mock 생성기를 사용한 전체 graph, 저장된 interrupt의 API projection/resume,
checkpoint 재개, 기본값 복원, 명시적 null, 중복 도구 주소, 잘못된 입력 차단. 실제 LLM/DB/원격 executor 통합은 별도입니다.

## 8. 계획 승인 v2 연결

v2 입력 제출이 성공하면 `plan_review_payload_version=2`를 기록합니다. `plan_review_v2.py`가
PlanReviewDisplay에 실제 compiled workflow, 확정된 execution_inputs, workflow_id/revision과 행동 목록을 전달합니다.
입력이 미확정이거나 workflow가 ready가 아니면 승인 화면을 열지 않습니다.

- action_name은 `workflow_approval`, allowed_decisions는 `approve`, `edit`입니다.
- 승인: API resume의 `action=approve`, payload 없음. 그래프 전달값은 `{"decisions":[{"type":"approve"}]}`입니다.
- 거절: `action=edit`, payload는 `{"intent":"reject","reason":"사유"}`이며 공백뿐인 사유는 허용하지 않습니다.
- 승인 화면은 읽기 전용입니다. 표시 객체를 제출하거나 approve에 변경값을 덧붙이는 직접 graph 제출은 거부합니다.
- 승인 시 기존 workflow와 execution_inputs를 그대로 보존하여 실행 경로로 넘깁니다. 재생성이나 LLM 입력 수정은 없습니다.
- 거절 사유는 workflow_rejected 컨텍스트로 기존 라우팅 에이전트에 전달합니다. 거절 자체를 즉시 분석 취소로 간주하지 않습니다.
- 버전 필드가 없는 기존 계획 승인 대기 checkpoint는 v1으로 재개합니다.

## 9. LangGraph dev에서 직접 확인

SSE 실제 문구와 채널의 전체 목록은 [프론트 문서의 메시지 전달 규칙](workflow_run_frontend.md)을 참조합니다.
코드 생성·실행 제출·조건부 판단·종료/중단 요청은 role=agent로 commentary에 전달합니다.
완료 결과·최종 보고서·사용자 요청 종료는 role=assistant로 final에 전달합니다.
화면용 진행 메시지는 graph_event_persistence에서 display_message로 전달하고 내부 로그도 보존합니다.
중간 상태 코드를 그대로 표시하지 않으며, 자동 제출 비활성 상태를 실제 실행 중으로 안내하지 않습니다.

현재 `langgraph.json`의 `agent`는 `langgraph_dev.py:graph`를 사용하므로 변경된 노드를 그대로 실행합니다.
저장된 이전 thread 대신 새 thread로 시작해야 세 단계 v2를 확인할 수 있습니다.

프로젝트 루트에서 PowerShell로 실행합니다. Windows 출력 인코딩 오류를 피하도록 UTF-8을 설정합니다.

```powershell
$env:PYTHONUTF8 = "1"
..\venv311\Scripts\langgraph.exe dev
```

현재 dev 설정은 실제 모델과 DB/Redis를 사용합니다. `.env`의 모델 설정,
`AGENT_CHECKPOINT_DATABASE_URL`, `EW_DATABASE_URL`, `EW_REDIS_URL` 및 해당 서비스가 준비되어야 합니다.
DB migration 등 초기 설정은 기존 개발 환경 설정을 따릅니다.
승인 이후 실제 실행까지 확인하려면 executor 연결도 필요합니다. HITL만 확인할 때는 승인 직전까지 진행할 수 있습니다.
이번 변경에서 dev 서버 기동과 실제 외부 서비스 연결까지 검증한 것은 아닙니다.

Studio의 최초 graph input 예시:

```json
{"user_request":"NCE 데이터 품질과 이상치 후보를 확인해줘"}
```

아래는 각 interrupt에 넣는 **resume 값 자체**입니다. 서비스 REST의 run_id/interaction_id를 포함한 요청 본문과 구분합니다.
LangGraph HTTP/SDK를 직접 호출한다면 이 값을 command.resume에 넣습니다.

후보 선택(candidate_id는 실제 표시된 값 사용):

```json
{"decisions":[{"type":"edit","edited_action":{"name":"workflow_candidate_selection","args":{"intent":"select","candidate_id":"generated_1"}}}]}
```

Agent Chat의 기본 편집 화면에서는 Candidates는 그대로 두고 **Available Actions** 칸의
배열 전체를 아래 객체로 바꾼 뒤 Submit합니다. candidate_id는 현재 표시된 후보 ID를 사용합니다.

```json
{"intent":"select","candidate_id":"generated_1"}
```

같은 칸에 `{"intent":"regenerate"}` 또는 `{"intent":"reject","reason":"변경 사유"}`를
넣어 해당 행동도 테스트할 수 있습니다. 현재 available_actions에 표시된 행동만 허용됩니다.
에이전트는 후보 선택 v2 노드에서 Agent Chat이 돌려준 표시 객체의 available_actions를
제출 객체로 변환한 뒤 기존 스키마로 검증합니다. 돌려받은 candidates는 실행 정의로 사용하지 않습니다.
서비스 API와 demo_v2.html의 제출 형식 및 검증은 그대로 유지합니다.

`langgraph_dev.py`는 `agent_chat_input_form=True` 실행 설정을 사용합니다. 이 설정에서
Tool input은 긴 tool_inputs JSON 대신 `step_0_tool_0_lot_cd`처럼 편집 가능한 인자별
텍스트 입력칸으로 표시됩니다. 누락값은 빈칸, 기존 값과 기본값은 미리 채워집니다.
문자열은 그대로 입력하고, 배열·객체·숫자·불리언은 JSON으로 입력합니다.
명시적 null은 `null`, 빈 문자열은 `""`로 입력합니다. 필수 인자의 빈칸은 오류이며,
선택 인자의 빈칸은 기본값으로 복원합니다. Tool output으로 연결된 인자는 표시하지 않습니다.
제출 후 기존 v2 payload로 변환하여 동일한 후보 workflow ID/revision·주소·타입 검증을 적용합니다.
일반 API 그래프는 이 설정을 사용하지 않으므로 demo_v2.html의 표시/제출 계약은 유지됩니다.
변경 전 Tool input 화면에서 이미 대기 중인 dev thread는 서버 재시작 후 기존 화면을
한 번 Submit하면 형식 오류 안내와 함께 새로운 인자별 입력 화면으로 다시 대기합니다.

Tool input은 표시된 workflow_id/revision을 복사하고, tool_inputs의 editable=true 인자 중 값이 있는 모든 인자를 제출합니다.
필수 인자는 누락할 수 없으며 선택 인자를 기본값으로 되돌릴 때는 해당 항목을 제외합니다.
아래는 제출 구조 예시이며 실제 필수 인자가 더 있다면 모두 추가해야 합니다.

```json
{"decisions":[{"type":"edit","edited_action":{"name":"additional_information","args":{"intent":"submit_inputs","workflow_id":"표시된 workflow_id","workflow_revision":1,"inputs":[{"step_index":0,"tool_index":0,"parameter":"data_type","value":"nce"}]}}}]}
```

계획 승인:

```json
{"decisions":[{"type":"approve"}]}
```

계획 거절(승인과 대체 관계):

```json
{"decisions":[{"type":"edit","edited_action":{"name":"workflow_approval","args":{"intent":"reject","reason":"분석 대상을 변경하고 싶습니다."}}}]}
```

Studio는 그래프 interrupt/resume와 상태 확인에 사용합니다. 서비스의 Task REST 응답 및 SSE envelope/channel은
별도 API 경로를 호출해서 확인해야 합니다. Studio 화면이 이 스키마로 제품용 폼을 자동 구성한다고 가정하지 않습니다.


## Tool input 출력 연결 편집 추가

새 Tool input 표시는 arguments.output_catalog에 스킬·Tool·반환값 목록을 한 번만 제공합니다.
각 반환값은 name/type/description/selectable을 가지며 프론트가 선행 호출과 selectable=true를 골라 표시합니다.
파라미터별 output_options와 스키마의 반복 후보 enum은 제거했습니다. 기존 tool_output 연결도 editable=true입니다.
resume inputs는 source=value/default/tool_output을 구분하며 tool_output은 reference를 받습니다.
공통 API envelope와 payload_version=2는 유지합니다. 기존 source 없는 value 요청도 지원합니다.
API와 에이전트는 저장된 output_catalog로 출처 존재·선행 순서·selectable을 검증합니다.
기존 pending의 output_options 방식도 지원하며 resume 형식은 유지합니다. 에이전트는 실행용 정의의 연결을 변경해
컴파일한 후 승인 단계에 전달합니다. 기존 binding 생략은 연결 유지이며,
기본값 전환은 source=default로 명시합니다. 기존 읽기 전용 pending 자료는 그대로 검증합니다.
상세 필드·제출 예시·드롭다운 구성은 [프론트 문서의 Tool 출력 연결 편집](workflow_run_frontend.md#tool-출력-연결-편집)을 참고하세요.
