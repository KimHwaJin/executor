# Workflow Run / Task API

> **API 경계 구현 업데이트 (2026-10-06):** v2 버전 보존·제출 검증·resume 변환을 구현했습니다.
> **에이전트 연결 상태:** 후보 선택·Tool input·계획 승인 모두 v2로 연결되었습니다. 추천 검색은 미구현입니다.
> 새 Workflow 1.0 선택부터 Tool input v2를 사용하고 기존 대기 checkpoint는 v1을 유지합니다.
> 에이전트 구현 범위는 [연결 현황](workflow_hitl_v2_agent_api_handoff.md#7-tool-input-노드-v2-연결)을 참고하세요.
> 정확한 적용 범위, F/E 기준, 검증 결과는 [API 검토·검증 결과](workflow_run_api_validation.md)를 우선 참고하세요.

> 2026-10-06: 공통 API는 유지하고 HITL 내부 계약을 설계 중입니다.
> [프론트 문서](workflow_run_frontend.md)의 **HITL 3종 전달 계약 초안**에 표시 정보와 행동별 제출 예시를 정리했습니다.
> 후보 선택·Tool input·계획 승인에 `payload_version=2` 내부 규격을 연결했습니다.

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
  "input": {"content": [{"type": "text", "text": "데이터 품질을 분석해줘"}]}
}
```

선택 필드 session_id가 있으면 해당 대화를 사용합니다. 없으면 선택 필드 project_id 또는
사용자 기본 프로젝트에 새 대화를 만듭니다. 기본 프로젝트도 없으면 409입니다.
현재 content는 비어 있지 않은 text 파트를 지원합니다. 원본 파트를 메시지에 보존하며,
모델 입력에는 줄바꿈으로 연결한 텍스트를 사용합니다.
모델 설정은 서버에서 관리합니다. 아래 메인 모델 설정 절을 참고하세요.

## 메인 모델 설정

Workflow Run 요청에서는 모델을 선택하지 않습니다. `main_model_name`과 `main_model`을
전달하면 422입니다. REST/SSE Task snapshot에도 모델 필드는 없습니다.

`src/agent_config.py`의 `USER_LLM_MODEL`에서 기본 모델을, `MAIN_MODEL_CONFIG`에서 파라미터를 설정합니다.

```python
USER_LLM_MODEL = "GaiA-Big-Latest"

MAIN_MODEL_CONFIG = {
    "parameters": {"temperature": 1.0, "reasoning_effort": "low"},
}
```

- `MAIN_MODEL_PRESETS`에 Big(GLM-5.3), Medium(GLM-5.3-Flash), Small(gaia-Qwen3.6-35B-A3B) 기본값이 있습니다.
- Alias를 사용하면 해당 기본 파라미터에 `parameters`를 덮어씁니다. 빈 객체면 프리셋 기본값을 사용합니다.
- 새 배포 모델 이름을 직접 지정할 수도 있습니다. 이 경우 필요한 파라미터를 직접 작성합니다.
- 표준 옵션은 SDK 인자로, 기타 옵션은 `extra_body`로 전달합니다. `enable_thinking`, `clear_thinking`은 `chat_template_kwargs`로 전달합니다.
- `max_output_tokens` 또는 `max_tokens`로 일반 호출 출력 상한을 지정할 수 있습니다. 생략하면 서버 기본값을 따릅니다.
- 모델 이름은 `LLM_MODEL_NAME` 환경변수가 우선이며, 없거나 공백이면 `USER_LLM_MODEL`을 사용합니다. Alias와 배포 모델 이름을 모두 지원합니다.
- 선택된 모델의 프리셋 기본값에 `MAIN_MODEL_CONFIG.parameters`를 덮어씁니다. 등록된 배포 모델 이름도 같은 프리셋 기본값을 사용합니다.
- `enable_thinking` 등 모델별 세부 옵션은 `MAIN_MODEL_PRESETS` 또는 `MAIN_MODEL_CONFIG.parameters`에서 관리합니다. `MODEL_TEMPERATURE`, `MODEL_ENABLE_THINKING` 환경변수는 에이전트 파라미터에 적용하지 않습니다. 연결 주소·인증·provider 설정은 환경변수를 유지합니다.
- 라우팅, 의도 분류, Skill 선택, 생성, FAQ, 조건 판단, 보고서, 후속 질문과 요약이 공통 설정을 사용합니다. 임베딩 모델은 별도 설정입니다.
- Run metadata나 checkpoint에 남은 과거 모델 선택값은 실행에 사용하지 않습니다. 재개도 현재 서버 설정을 사용합니다.
- 설정 변경 후 API와 Executor 이벤트 Worker를 모두 동일한 설정으로 재시작합니다.
- 컨텍스트 입력 예산과 요약 임계값은 기존 두 환경변수로 관리합니다. [컨텍스트 정책](session_context.md)을 참고하세요.

## Task snapshot

다음 ID 문자열은 설명용이며 실제로는 UUID입니다.

```json
{
  "schema_version": 1,
  "task_id": "T1",
  "session_id": "S1",
  "current_run_id": "R1",
  "submission_run_id": "R1",
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

### 실행기 결과를 받은 뒤의 종료 처리

Executor Worker가 그래프를 재개한 결과도 기존 메시지 저장 및 Task SSE 경로로 전달합니다.
실행 실패 시 오류 안내를 `message.completed` (`channel=final`)로 저장한 뒤,
Run/Task를 `error`로 갱신하고 `task.error`를 저장합니다. Task 상태와 종료 이벤트는 같은 transaction으로 반영합니다.
SSE는 저장된 이벤트를 전달하고 `task.snapshot` (`status=error`, `task_finished=true`) 및
`stream.closed` (`reason=task_finished`)를 보냅니다. 이벤트 타입과 envelope는 추가하지 않습니다.

- 성공은 `success` / `task.success`, 일반 취소는 `canceled` / `task.canceled`로 종료합니다.
- 실행 실패, 실패한 셀, 결과 수집 오류는 `error`로 처리합니다. 실행기가 `SUCCEEDED`여도 결과 수집에 실패하면 Task는 `error`이며 `failure.execution_status`에는 원래 실행기 상태를 보존합니다.
- MULTI 중간 결과 이후 다음 실행기 이벤트를 기다리는 동안에는 `running`, `phase=executor_wait`, `task_finished=false`를 유지합니다.
- Worker 재전달 시 체크포인트 처리 완료 기록이 있으면 그래프를 다시 실행하지 않고 누락된 DB 반영을 재시도합니다. 메시지는 기존 식별자로 중복을 방지하고 완료된 Run에는 종료 이벤트를 다시 추가하지 않습니다.

실행 실패 예시는 [04_execution_failed.json](examples/workflow_hitl_v2/04_execution_failed.json)에 있습니다.
이 예시는 `rest_response.body`에 최종 GET snapshot, `sse_events`에 순서대로 전달되는 SSE envelope를 담습니다.
기존 선택·입력·승인 예시 4개의 계약은 변경하지 않습니다.

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

## HITL 내부 계약 (단계별 연결 중, 2026-10-06)

정의의 기준 문서는 [프론트 계약](workflow_run_frontend.md)의 **HITL 3종 전달 계약 초안**입니다.
신규 엔드포인트나 REST 응답을 만들지 않고 기존 InteractionResource 내부를 버전별로 정의합니다.

| HITL kind | 제안 payload_version | 표시 자료 (`payload.arguments`) | 재개 |
| --- | --- | --- | --- |
| `workflow_candidate_selection` | 2 | 후보 목록, 출처, 이유, 전체 workflow, 추천 메타데이터·검색 결과, available_actions | edit + intent별 payload |
| `additional_information` | 2 | workflow, revision, 도구별 전체 인자·기본값·현재 값·출처 | edit + 호출 위치별 inputs |
| `plan_review` | 2 | 완성된 workflow, revision, execution_inputs | approve 또는 edit + {intent: reject, reason} |

`schema_version=1`인 공통 Task/SSE envelope는 유지합니다. payload_version은 interaction 계약 버전이고,
workflow_revision은 검토 대상 워크플로우 버전입니다. interaction의 revision과 혼용하지 않습니다.
클라이언트가 resume에 payload_version을 추가하지 않으며 서버가 저장된 interaction에서 버전을 결정합니다.

공통 resume_target, 멱등 키, REST 202, Task SSE 재사용 규칙은 같습니다.
표시 자료와 제출 schema를 분리하고, 같은 정의에서 동적 actions/허용 intent/JSON Schema를 생성해야 합니다.
임의의 dict를 허용한다는 뜻은 아닙니다. 행동별 스키마 및 저장된 후보·도구 주소·계획 버전 검증이 필요합니다.
두 거절 모두 edit + payload의 intent=reject와 필수 reason으로 통일하는 매핑을 제안합니다.
현재 일반 action=reject는 feedback을 읽으므로 v2 reason 전달에 사용하지 않습니다.
계획 검토의 허용 action과 edit schema, 에이전트의 해석을 함께 변경해야 합니다.
새로운 필드 오류 코드나 별도의 message REST 응답은 이 초안에서 추가하지 않습니다.

재추천 응답은 기존 추천 후보 + 신규 추천 후보의 누적 목록입니다. 기존 workflow_id는 신규 검색에서만 제외하며,
기존 candidate_id와 버전 참조를 유지합니다. 최초/추가 검색마다 `WORKFLOW_RECOMMENDATION_LIMIT`개(기본 3)를
최대로 찾으므로 누적 목록의 길이는 3개를 넘을 수 있습니다. 기준은 `WORKFLOW_SIMILARITY_SCORE` 이상(기본 0.8)입니다.
재생성은 기존 생성 후보 구조를 제외하고 `WORKFLOW_GENERATION_LIMIT`개(기본 2, 허용 1..20)의 새 후보를 목표로 교체합니다.
추가 후보가 재시도 후에도 중복이면 유효한 생성 후보만 반환할 수 있습니다. 기존 candidates[].reason과
에이전트 메시지에 요청 개수·실제 개수 및 중복 안내를 포함하며 별도 응답 필드는 추가하지 않습니다.

추천 후보의 선택 필드는 `workflow_id`, `version_id`, `revision`, `similarity_score`입니다.
`revision`은 저장 문서 버전이며 공통 interaction/resume revision과 혼용하지 않습니다.
`payload.arguments.recommendation_search`는 다음 선택 필드를 추가합니다. 생성 후보에서는 생략할 수 있습니다.

| 필드 | 의미 |
| --- | --- |
| `status` | `matched`: 신규 후보 있음 / `no_additional_matches`: 기존 후보는 있으나 추가 후보 없음 / `no_matches`: 최초 검색 후보 없음 |
| `added_candidate_ids` | 이번 응답에서 신규 추가된 candidate_id 배열. 최초 추천에서는 전체 후보 ID, 추가 후보 없음이면 빈 배열 |
| `message` | 표시용 안내 문자열 또는 null |

`no_additional_matches`에서는 기존 후보와 선택 행동을 유지하고 안내만 표시합니다. 자동 생성으로 전환하지 않습니다.
최초 검색에 후보가 없는 `no_matches`이면 에이전트가 생성으로 진행합니다.
프론트는 매 응답의 누적 목록으로 갱신하고, 현재 added_candidate_ids에만 새 후보 표시를 합니다.
유사도·문서 버전이 없는 이전 응답은 해당 표시를 생략합니다.
새 엔드포인트나 공통 envelope 변경 없이 REST interaction 및 SSE interaction.opened에 동일하게 전달합니다.
현재 두 요청은 intent만 제출하고 feedback은 향후 수집을 위한 선택 필드로 둡니다.
후보 거절은 현재 interaction의 전체 목록을 대상으로 하는 필수 reason이며,
계획 거절도 필수 reason입니다. 두 경우 모두 에이전트가 새 워크플로우 요청인지,
분석 자체의 중단 의사인지 등 이유와 맥락을 해석해 후속 경로를 판단합니다.
API는 reject만으로 Task를 취소하지 않으며, 에이전트의 명시적인 중단 판단과 기존 종료 처리를 연결해야 합니다.
후보 제외 이력은 서버에서 관리하고 클라이언트 제출값에 의존하지 않습니다.
Tool input 제출 후 에이전트는 입력 반영·기본값 적용·검증만 수행하고 계획이나 입력값을 추가 변경하지 않습니다.
완성된 계획을 읽기 전용으로 보여주고, 승인 시 표시한 내용 그대로 실행합니다. 자동 변경·재승인 루프는 이번 범위에서 제외합니다.

## 검토용 전체 JSON 쌍

추천 후보 화면에 `generate`(워크플로우 생성하기)를 추가했습니다.
resume는 기존 action=edit와 `payload={"intent":"generate"}`를 사용합니다.
공통 제출 union의 CandidateGeneration 모델로 검증하며, 현재 interaction에 제공된 경우에만 허용합니다.
생성 후보 화면의 regenerate와는 다른 동작입니다.

프론트 문서의 **전체 응답과 resume 요청 쌍**에서 필드 위치·분기·기본값 처리 규칙을 설명합니다.

| 상황 | 전체 JSON 파일 |
| --- | --- |
| 추천 후보 선택·누적 재추천·추가 후보 없음·거절 | [01_recommended_selection.json](examples/workflow_hitl_v2/01_recommended_selection.json) |
| 생성 후보 선택 및 재생성·거절 | [01_generated_selection.json](examples/workflow_hitl_v2/01_generated_selection.json) |
| Tool input 값·기본값·출력 연결 제출 | [02_tool_inputs.json](examples/workflow_hitl_v2/02_tool_inputs.json) |
| 완성된 계획 승인 및 거절 | [03_plan_review.json](examples/workflow_hitl_v2/03_plan_review.json) |

파일별 rest_response.body는 GET snapshot, sse_response.data는 SSE envelope,
resume_requests의 각 body는 POST 요청, resume_accepted_response.body는 202 응답입니다.
파일은 문서용 묶음이며 새로운 API envelope가 아닙니다. 설명용 demo skill/tool 대신 실제 interaction에 포함된 후보·도구·식별자를 사용해야 합니다.
선택→입력→승인의 기본 경로는 recommended.select → tool_inputs.change_output_reference → plan_review.approve입니다.

추천 JSON의 `rerecommendation_examples`는 후속 interaction과 선택 요청을 묶은 문서용 예시입니다.
`interaction` 객체는 REST `interactions[]` / SSE `data.data`에 해당하며 새 API 최상위 필드가 아닙니다.
생성 JSON의 단일 후보는 `WORKFLOW_GENERATION_LIMIT=1` 예시입니다. 기본 2개 및 다른 설정값도 같은 후보 배열 계약을 사용합니다.
나머지 요청은 같은 interaction에 연달아 보내는 요청이 아니라 서로 대체하는 선택지입니다.

API 담당자 확인 사항:

- payload_version=2를 저장 interrupt부터 공개 interaction 및 resume 해석까지 보존할 위치.
- plan_review에 approve/edit를 허용하고 edit를 intent=reject와 reason 스키마로 제한하는 매핑.
- 표시 arguments와 제출 payload의 모델 분리. arguments 전체를 제출 schema로 검증하지 않음.
- Draft 2020-12 oneOf/const/contains/minContains/maxContains 지원 및 스키마/도메인 검증의 오류 매핑.
- 워크플로우 버전 불일치를 409로 처리하고 스키마·주소·필수값 오류를 422로 처리하는 검증 순서.
- 선택 인자 생략 시 기존 값 유지가 아닌 도구 기본값 복원. 값 반영·컴파일은 에이전트 담당과 연결.
- 인터럽트 대기 상태와 입력 자료가 REST/SSE에서 동일하게 복원되고 reason 거절을 즉시 Task 취소로 바꾸지 않는지.


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
