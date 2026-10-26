# Workflow Run 동작 원리

`POST /api/v1/workflow_run`은 그래프를 실행하지 않습니다. 요청을 검사하고 DB에 Task와 Run을 `pending`으로 넣은 뒤 202로 snapshot을 돌려줍니다. 그래프는 `run-worker`가 그 줄을 집어 가서 실행합니다.

화면 계약은 `docs/workflow_run_frontend.md`에 있습니다. 이 문서는 그 요청이 서버 안에서 어떻게 이어지는지입니다.

## 1. 요청이 들어오기 전에

모든 호출은 `X-User-ID`로 활성 사용자를 확인합니다. 없거나 정지된 사용자면 401이고, 이 단계에서는 DB에 아무것도 쓰지 않습니다.

body는 `WorkflowRunRequest`입니다. 없는 필드는 422입니다. `input`과 `command`는 둘 중 하나만 있어야 합니다.

- 새 메시지: `input`만 있습니다. `task_id`, `run_id`가 있으면 422입니다.
- 재개: `command.resume`과 `task_id`, `run_id`가 있어야 합니다. `session_id`, `project_id`가 있으면 422입니다. 대화는 `run_id`로 서버가 찾습니다.

`Idempotency-Key`는 1자에서 255자여야 합니다. `main_model_name`은 `default`만 됩니다.

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
