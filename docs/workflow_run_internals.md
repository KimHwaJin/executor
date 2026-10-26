# Workflow Run 동작 원리

> **API 경계 구현 업데이트 (2026-10-06):** v2 버전 보존·제출 검증·resume 변환을 구현했습니다.
> **에이전트 연결 상태:** 후보 선택·Tool input·계획 승인 모두 v2로 연결되었습니다. 추천 검색은 미구현입니다.
> 새 Workflow 1.0 선택부터 Tool input v2를 사용하고 기존 대기 checkpoint는 v1을 유지합니다.
> 에이전트 구현 범위는 [연결 현황](workflow_hitl_v2_agent_api_handoff.md#7-tool-input-노드-v2-연결)을 참고하세요.
> 정확한 적용 범위, F/E 기준, 검증 결과는 [API 검토·검증 결과](workflow_run_api_validation.md)를 우선 참고하세요.

> 아래 본문은 현재 구현입니다. 2026-10-06 HITL 초안의 구현 변경 지점은 이 문서 끝에 별도로 기록합니다.

`POST /api/v1/workflow_run`은 그래프를 실행하지 않습니다. 요청을 검사하고 DB에 Task와 Run을 `pending`으로 넣은 뒤 202로 snapshot을 돌려줍니다. 그래프는 `run-worker`가 그 줄을 집어 가서 실행합니다.

화면 계약은 `docs/workflow_run_frontend.md`에 있습니다. 이 문서는 그 요청이 서버 안에서 어떻게 이어지는지입니다.

## 1. 요청이 들어오기 전에

모든 호출은 `X-User-ID`로 활성 사용자를 확인합니다. 없거나 정지된 사용자면 401이고, 이 단계에서는 DB에 아무것도 쓰지 않습니다.

body는 `WorkflowRunRequest`입니다. 없는 필드는 422입니다. `input`과 `command`는 둘 중 하나만 있어야 합니다.

- 새 메시지: `input`만 있습니다. `task_id`, `run_id`가 있으면 422입니다.
- 재개: `command.resume`과 `task_id`, `run_id`가 있어야 합니다. `session_id`, `project_id`가 있으면 422입니다. 대화는 `run_id`로 서버가 찾습니다.

`Idempotency-Key`는 1자에서 255자여야 합니다. 모델과 파라미터는 서버의 `agent_config.py`에서 설정합니다.

헤더 `X-DTest-Mock-LLM: 1`이 있으면 metadata에 mock 표시를 넣습니다. 부하 테스트용이고, 화면 계약은 아닙니다.

진입점은 `src/app/api/v1/routes/workflow_run.py`의 `submission`입니다. 인증과 body 검사 뒤 `submit_workflow`를 호출합니다.

## 2. 같은 요청을 두 번 내면

`submit_workflow`(`src/app/services/workflow_run_service.py`)는 사용자 행을 `FOR UPDATE`로 잠급니다. 같은 사용자의 제출이 동시에 두 갈래로 들어가지 않게 하려는 잠금입니다.

키는 그대로 저장하지 않고 `workflow_run:`에 키의 SHA-256을 붙인 값으로 Run을 찾습니다. body 전체도 SHA-256으로 fingerprint를 만듭니다.

이미 그 키가 있으면 body fingerprint가 같을 때만 기존 Run을 그대로 반환합니다. body가 다르면 409입니다. 이 재전송은 REST와 `POST /workflow_run/stream`이 같은 키 공간을 씁니다. 그래프를 다시 돌리지 않습니다.

## 3. 새 메시지

`session_id`가 없으면 `project_id` 또는 기본 프로젝트에 세션을 만듭니다. 기본 프로젝트도 없으면 409입니다.

그 세션에 사용자 메시지를 먼저 저장합니다. `content`의 텍스트를 줄바꿈으로 이어 `content_text`로 두고, 원본 part도 같이 남깁니다. 이 메시지 ID가 이번 Run의 `trigger_message_id`가 됩니다.

그다음 `RunService.create`에 그래프 입력을 넘깁니다. 그래프에 들어가는 문장은 `role: user`와 그 텍스트입니다.

## 4. 재개면, 그 전에 답변을 그래프 명령으로 바꿈

재개는 기존 Run이 아직 `interrupted`이고, 그 Task의 최신 Run일 때만 됩니다. 아니면 409 `Run is stale or task is not waiting for input.`입니다.

`resume_command`가 화면의 `command.resume`을 LangGraph decision으로 바꿉니다.

1. `interaction_id`와 `revision`이 그 Run의 interrupt와 맞는지 봅니다. 틀리면 409입니다.
2. interrupt는 action 하나, review config 하나여야 합니다. Executor 대기처럼 그 모양이 아니면 409입니다.
3. `action`이 허용 목록에 있어야 합니다. 도메인 이름(예: `workflow_approval`)을 보내면, `edit`가 허용돼 있으면 edit로, 아니면 respond로 바꿉니다.
4. `approve`는 클라이언트가 보낸 값을 쓰지 않습니다. 서버에 저장된 기본 args로 `{"type":"approve"}`를 만듭니다. `payload`가 있으면 422입니다.
5. `edit`는 `payload` 객체를 JSON Schema로 검사합니다. 실패하면 422 `INVALID_INTERACTION_INPUT`입니다. `readOnly` 필드를 바꾸면 422 `READ_ONLY_INPUT`입니다.
6. `reject`와 `respond`는 문자열 메시지입니다.

이렇게 만든 `{"decisions":[...]}`가 새 Run의 `command_json`이 됩니다. 화면이 보낸 resume 원문도 metadata `workflow_resume`에 남습니다.

## 5. DB에 줄을 넣고 바로 반환

`RunService.create`의 일반 호출은 실행하지 않고 큐에 넣습니다. 구현은 `src/app/services/run_service.py`입니다.

새 메시지면 Task를 하나 만듭니다. 상태는 `pending`, 주인은 `queue:unclaimed`입니다. 세션당 `pending`/`running` Task는 하나라서, 이미 있으면 unique 제약으로 409 `An active task already exists for this session.`이 납니다.

재개면 새 Task를 만들지 않습니다. 기존 Task를 잠그고, `waiting_input`인지 확인한 뒤 다시 `pending`으로 바꿉니다. 이전 실행의 lease, heartbeat, lock token은 지웁니다. 취소가 이미 요청됐으면 409입니다. interrupt 안에 `EXECUTOR_EVENT`가 있으면 409 `Executor is running`입니다.

그 Task에 Run 행을 추가합니다. 상태 `pending`, 입력 또는 command, metadata, 멱등 키를 넣습니다. 첫 Run이면 그 Run ID가 Task의 `root_run_id`이자 `checkpoint_run_id`입니다. 재개로 Run이 새로 생겨도 LangGraph thread는 이 checkpoint Run을 계속 씁니다.

재개면 이전 공개 interaction마다 `interaction.submitted` 이벤트를 같은 트랜잭션에 넣습니다. 그리고 `task.queued`를 넣습니다. 이 이벤트는 commit 전에 끝나야 합니다. commit 뒤에 넣으면 워커의 lease 갱신과 락 순서가 꼬입니다.

commit이 성공하면 API는 여기서 리턴합니다. `create_workflow_run`이 `Location: /api/v1/tasks/{task_id}`를 달고, 그 Task의 snapshot을 읽어 `submission_run_id`만 이번 Run ID로 덮어 202로 보냅니다. 이때 그래프는 아직 안 돌았으므로 snapshot은 보통 `pending`입니다.

`POST /workflow_run/stream`은 같은 `submission`을 한 뒤, 202 대신 그 Task의 SSE를 바로 엽니다. 헤더에 `X-Task-ID`, `X-Run-ID`가 있습니다. cursor가 잘못되면 Run을 만들기 전에 400으로 끝냅니다.

## 6. 워커가 집어 감

`run-worker`는 `AGENT_RUN_WORKER_CONCURRENCY`개의 루프가 `claim_one`을 반복합니다. 구현은 `src/app/agent_run_worker.py`와 `src/app/agent_run_worker_parallel.py`입니다.

`claim_one`은 `pending`이고 재시도 시각이 된 Run을 `created_at` 순으로 하나 집습니다. `FOR UPDATE SKIP LOCKED`라서 루프끼리, 프로세스끼리 같은 줄을 집지 않습니다.

Task도 잠급니다. Task가 `pending`이 아니면 그 Run을 error로 닫고 넘어갑니다. 정상이면 Task에 실행 lease를 걸고 Run을 `running`으로 바꿉니다. `attempt_count`를 1 올리고 `started_at`을 찍은 뒤 commit합니다. 큐 대기는 이 `started_at - created_at`입니다.

그다음 `execute_claimed`가 다른 DB 세션으로 `RunService.create(..., _execute_existing=True)`를 호출합니다. 같은 멱등 키의 Run이 이미 있으므로 줄을 새로 만들지 않고, 이 분기에서만 그래프를 실행합니다.

실행 시작 이벤트는 새 메시지면 `task.started`, 재개면 `task.resumed`입니다. 실행 중에는 lease heartbeat가 돕니다. 새 메시지는 `ainvoke_user_turn`, 재개는 `ainvoke_resume`입니다. resume은 checkpoint Run의 thread에 decision을 넣어 멈춘 노드 다음으로 갑니다.

## 7. 그래프가 멈추거나 끝날 때

그래프가 사람 입력을 기다리면 LangGraph interrupt가 state에 남습니다. 워커는 그걸 Run의 `interrupt`에 저장하고 Run 상태를 `interrupted`로 둡니다. 분석 경로면 Task는 `waiting_input`이 되고, 실행 lease를 풉니다. 그래서 기다리는 동안 워커 슬롯을 잡지 않습니다.

공개 폼은 이 interrupt를 `public_interactions`(`src/app/services/workflow_interactions.py`)로 거른 결과입니다.

- `EXECUTOR_EVENT`와 `next_user_request`는 뺍니다.
- action 하나, review config 하나인 것만 남깁니다.
- `workflow_approval`은 `kind=plan_review`, `analysis_context`는 `kind=planning_question`이 되고, 나머지는 action 이름 그대로입니다.
- `interaction_id`는 `run_id`와 interrupt 순서로 만든 UUID라서 프로세스가 재시작돼도 같습니다. `revision`은 1입니다.

각 항목을 `interaction.opened`로 남기고 `task.waiting_input` 이벤트를 남긴 뒤 commit합니다. SSE는 이 줄을 읽어 폼을 엽니다.

Executor에 넘긴 직후면 interrupt에 `EXECUTOR_EVENT`가 있습니다. Run 상태는 `running`으로 두고, Task도 `running`인 채 lease만 풀고 `lock_owner`를 executor 대기로 바꿉니다. 세션의 활성 Task 자리는 계속 이 Task가 잡고 있습니다. 공개 interaction은 없으므로 폼이 없고 `phase`는 `executor_wait`입니다.

분석이 아니면, interrupt가 `next_user_request`만 있을 때 Run을 `success`로 덮어 씁니다. FAQ나 파일 조회가 끝난 뒤 다음 말을 같은 Task의 폼으로 열지 않기 위해서입니다. Task와 이벤트는 지우지 않고 완료로 남습니다. 다음 사용자 말은 새 Task입니다.

성공, 에러, 타임아웃, 취소도 같은 자리에서 Task를 그 상태로 바꾸고 `task.success` 같은 이벤트를 남깁니다. 재시도 가능한 예외이고 횟수가 남았으면 Run을 다시 `pending`으로 두고 `task.retry_scheduled`만 남깁니다. 같은 Run의 `attempt_count`만 올라갑니다.

## 8. 화면이 상태를 읽는 방법

`GET /tasks/{task_id}`는 Task와 그 Task의 최신 Run을 한 번에 읽어 snapshot을 만듭니다. 구현은 `src/app/services/workflow_task_service.py`의 `build_snapshot`입니다.

- `input_required`와 `can_resume`은 공개 interaction이 있을 때만 참입니다.
- `can_cancel`은 Task가 끝나지 않았고, 취소 요청이 없고, executor 대기가 아닐 때 참입니다.
- `phase`는 열린 interaction의 `kind`, executor 대기면 `executor_wait`, 아니면 status입니다.
- `result`는 Task가 `success`일 때만 Run의 `agent_response`입니다.

`GET /tasks/{task_id}/stream`은 그래프에 붙어 있지 않습니다. `src/app/services/workflow_task_stream.py`가 DB를 짧게 폴링합니다. 새 이벤트가 있으면 `id`와 envelope로 보내고, 상태가 바뀌면 `task.snapshot`을 보냅니다. Task가 끝나면 `stream.closed` `task_finished`로 닫습니다. 입력을 기다리는 동안에는 닫지 않습니다. 연결이 끊겨도 워커는 계속 돕니다.

`GET /workflow_run/{run_id}`는 그 Run 한 건의 상세입니다. 여기 interactions는 공개 폼으로 거르기 전입니다. 화면 폼은 Task snapshot 쪽을 씁니다. `GET /workflow_run/{run_id}/stream`은 그 Run의 status와 end만 보는 보조 스트림입니다.

사용자가 폼을 제출하면 1번부터 다시입니다. 같은 Task에 Run만 하나 더 `pending`으로 생기고, 워커가 checkpoint thread를 이어서 실행합니다.

## 9. HITL 3종의 구현 연결 지점 (단계별 연결 중, 2026-10-06)

공개 규격의 기준은 [프론트 계약](workflow_run_frontend.md)의 **HITL 3종 전달 계약 초안**입니다.
이 문서 변경은 런타임 구현 변경이 아닙니다. 현재 구현과 필요한 작업은 다음과 같습니다.

| 지점 | 현재 | 필요한 작업 |
| --- | --- | --- |
| `schemas/common/workflow_task_schema.py` | FormPayload의 arguments/input_schema가 dict | HITL별 표시/제출 모델을 별도 정의하고 기존 envelope에 직렬화 |
| `graphs/nodes/hitl.py` 후보 선택 | candidate_number와 Markdown overview | 구조화 후보 목록과 intent별 응답 검증·분기 |
| `graphs/nodes/workflow_inputs.py` | unresolved input만 폼에 포함 | 전체 도구 인자와 값 출처 제공, 호출 위치별 제출 처리 |
| `services/workflow_interactions.py` | payload_version 기본 1, 전송 action label 고정 | 저장된 계약 버전 보존, 표시 자료와 허용 intent 투영 |
| `services/workflow_run_service.py` | edit JSON Schema 검증, 일반 approve/reject 변환 | 버전별 도메인 검증, 거절 이유 필수 검증, 저장된 대상/버전 검증 |
| `graphs/hitl_protocol.py` 및 각 노드 | approve는 args, edit는 submitted args로 unwrap | 표시용 args와 제출값의 형태 차이를 각 계약에 맞게 처리 |
| `services/chat_crud_message_sink.py` | 저장 메시지 유형에서 commentary/final 결정 | 노드별 사용자 문장·타입이 의도한 공개 채널로 투영되는지 검증 |

권장 모델 경계는 후보 표시/선택 제출, 도구 인자 표시/호출 위치별 제출, 계획 표시입니다.
선택 제출은 intent로 구분하는 union 모델로 정의합니다. 도구 입력은 도구 등록 정보의 타입·기본값과
workflow binding, 사용자 발화에서 추출한 값, 사용자 확정 값을 결합해 생성합니다.
기존 extract_workflow_inputs는 input source 추출에 사용되므로, 전체 인자를 보여주려면 도구 등록 정보와
literal/tool_output 연결까지 함께 조회해야 합니다. 기존 미해결 입력 목록만 확장해서는 요구를 충족하지 않습니다.

표시 객체 전체를 edit 입력 schema의 대상으로 만들지 않습니다. args는 화면 자료이고,
args_schema는 resume payload만 검증하도록 정의합니다. 현재 readOnly 비교는 최상위 필드만 다루므로
후보 ID와 중첩 입력 주소/수정 권한 검증을 대체하지 못합니다.
approve는 서버에 저장한 승인용 args를 사용하도록 유지하고 계획 버전에 묶어야 합니다.

API 검증에 실패하면 새 Run을 만들기 전에 422 또는 오래된 대상에 대한 409로 응답하도록 설계합니다.
그래프의 컴파일 단계에서야 발견한 도메인 오류는 이미 수락된 POST의 422로 되돌릴 수 없습니다.
그 경우 입력을 보존해 새 입력 interaction과 오류 설명을 제공하는 후속 처리가 필요합니다.
검증 이후에만 execution_inputs를 적용하며 재사용 workflow 정의와 실행 입력의 기존 분리를 유지합니다.

새 계약은 kind/payload_version으로 구분합니다. 기존 저장 interrupt에 버전 정보가 없으면 version 1로 처리하고,
version 2 전용 노드·투영·재개 처리 준비 후 발행합니다. 공통 envelope를 바꾸지 않아도 이 연결 작업은 필요합니다.
재추천·재생성·거절 이후 라우팅은 interaction 모델과 분리하고, 미정 경로를 자동 생성/취소로 가정하지 않습니다.
재추천은 기존 추천 후보를 제외하고, 재생성은 기존 생성 후보를 제외한 새 후보 1개를 생성합니다.
제외 이력은 그래프/서버 상태로 보존합니다. 등록 후보의 실제 식별자 및 생성 정의를 비교하는 전략과
이력 유지 범위는 추가 설계하며, candidate_id 재발급만으로 제외 조건을 충족했다고 보지 않습니다.
현재 재추천/재생성은 feedback 없이 처리하고 선택 feedback 필드는 향후 확장을 위해 지원하도록 설계합니다.
후보 거절의 대상은 저장된 현재 후보 전체입니다. 후보 거절과 계획 거절 모두 필수 reason을 받아
새 워크플로우 요청 등 워크플로우 관련 의도와 분석 자체의 중단 의사를 구분해 후속 경로를 판단합니다.
v2에서는 두 거절 모두 action=edit, payload={intent: reject, reason: ...}으로 받는 매핑을 제안합니다.
계획 검토의 edit 허용 및 전용 schema를 정의하고, 에이전트가 이 payload를 거절로 해석하도록 수정해야 합니다.
version 1의 action=reject/feedback 처리는 호환 유지합니다. 판단 결과와 근거를 서버 상태에 남기는 것을 제안합니다.
의도 불명확 시 확인을 요청하는 경로와, 중단 판단을 기존 Task 종료/취소 처리에 연결하는 작업은 별도 합의가 필요합니다.

구현 시 검증 범위:

- 후보 출처와 무관한 선택, 없는 후보/허용하지 않은 intent 거부, 공백뿐인 거절 사유 거부.
- 재추천/재생성의 기존 후보 제외, 생성 결과 1개, feedback 생략 허용, 후보 목록 전체 거절 범위 확인.
- 계획 거절과 분석 중단 의도의 분기 검증, 모호한 이유를 임의로 종료로 처리하지 않는지 확인.
- 동일 skill/tool 반복 호출의 주소 구분, 전체 필수/선택 인자 표시, 사용자 값 우선과 기본값 적용.
- 0/false/null/미입력 구분, 중복 주소·읽기 전용 변경·필수 누락·잘못된 타입 거부.
- 오래된 workflow_revision 거부, Tool input 반영 후 에이전트의 추가 변경 없음, 승인 화면과 실행 내용 일치, 거절 시 실행 금지.
- 동일 interaction의 REST/SSE 일치, 재연결 복원, 제출 중복 방지, version 1 대기 건 호환.
- 실제 처리 시점과 commentary 일치, final 메시지와 Task 완료 상태의 분리.

## 10. 요청·응답 예시 연결

[프론트 문서](workflow_run_frontend.md)의 **전체 응답과 resume 요청 쌍**과
`docs/examples/workflow_hitl_v2/*.json`은 계약 검토용 자료입니다. 런타임 모델이나 노드는 변경하지 않았습니다.
JSON 파일에는 실제 공통 envelope 형태의 전체 REST/SSE와 각 행동별 resume가 포함됩니다.
설명용 demo 도구는 등록하지 않았으며 실제 워크플로우 실행 가능성을 검증한 자료가 아닙니다.

기본 경로에서 추천 첫 후보의 workflow, Tool input의 workflow, 계획 승인의 workflow는 동일합니다.
Tool input submit_all의 네 값은 계획 execution_inputs와 일치합니다. clear_optional_to_defaults 경로라면
method=iqr, threshold=None으로 입력을 확정해야 하므로 해당 분기의 계획 자료를 다시 만들어야 합니다.
이는 제출에 따른 결정적 값 반영이며 에이전트의 자동 계획 수정이나 재승인 루프가 아닙니다.

input_schema는 제출 payload만 검증하고, API의 오래된 interaction/워크플로우 검사 및 도메인 검증을 대체하지 않습니다.
계획 승인 action은 payload를 받지 않고, edit action은 reason 거절만 받도록 노드와 검증 계층을 함께 맞춥니다.
같은 화면의 여러 resume 예시는 상호 배타적입니다. 하나 수락 후 나머지를 같은 interaction에 제출하면 안 됩니다.


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
