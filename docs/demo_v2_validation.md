# demo_v2 HITL 연동·검증 결과

검토 기준: `654e046`까지 반영된 동료의 HITL v2 구현과 공통 스키마.
후보 선택 → Tool input → 계획 승인 연결을 기준으로 데모를 수정했습니다.
추천 검색은 미구현이므로 생성 후보를 사용했습니다.

## 스키마·API 검토

공통 제출/표시 모델과 동적 스키마 생성기는 그대로 사용할 수 있어 필드·타입을 변경하지 않았습니다.
`generate`를 포함한 행동은 `available_actions`와 transport `actions`를 함께 확인합니다.
API에는 v2 계획 승인 시 저장된 `available_actions`에도 approve가 있는지 검사하는 조건을 추가했습니다.
허용되지 않은 승인은 새 Run 생성 전 422입니다. v1 처리와 task/run/message 서비스는 변경하지 않았습니다.

## 데모 변경

- 서버가 제공한 행동·label·candidate_id·resume_target으로 폼과 제출값을 구성합니다.
- Tool input은 현재 값, 사전 입력값의 출처, 기본값, 필수/선택, 읽기 전용 여부를 표시합니다.
- workflow literal은 `editable`에 따라 수정 가능하고, 이전 tool 출력 연결은 읽기 전용이며 제출에서 제외합니다.
- 스키마·업무 규칙 검증은 API에서만 수행합니다. HTML의 검증기와 제출 전 필수값·타입·enum·거절 사유 검사를 제거했습니다.
  화면은 스키마를 위젯 표시와 JSON 직렬화에만 참고합니다. 잘못된 타입도 제출하여 API의 422 응답을 확인할 수 있습니다.
  HTTP 상태, 서버 detail/errors, request_id를 표시하며 422/409에서 작성값을 유지합니다.
- boolean/enum의 타입을 보존하며 복합 타입은 JSON 입력으로 받습니다.
  0, false, null, 빈 문자열을 보존하고, 선택 인자 생략은 기본값 복원 요청으로 전송합니다.
- 계획 화면은 확정 workflow 문서를 펼쳐 표시하고 실행 입력값을 함께 보여줍니다.
  approve에는 payload를 넣지 않습니다. 거절 사유가 공백인지 여부는 API가 검사합니다.
- 제출 중 폼을 잠그고 `interaction.submitted` 또는 수락 응답으로 제출한 폼을 다시 열지 않습니다.
- 같은 interaction의 snapshot 재수신 시 작성값을 유지합니다. 오래된 snapshot은 무시합니다.
  409에서 현재 폼이 바뀌면 이전 제출값을 별도 보관·표시하며 자동 재제출하지 않습니다.
- `message.completed.data.channel`로 commentary/final을 표시하고 message_id로 중복을 제거합니다.
  REST 메시지 복원에서도 named assistant를 agent로 잘못 분류하지 않습니다.
- POST SSE 재개 시 동일 Task cursor를 지우지 않습니다. resync_required는 snapshot 복원 후 재연결합니다.
- 일반 HTTP 개발 환경에서도 멱등 키를 생성할 수 있도록 crypto.randomUUID의 대체 경로를 추가했습니다.

화면 표시·입력 수집·API/SSE 통신만 `demo_v2.html`에 포함했습니다.
브라우저 스키마 검증 라이브러리 및 빌드 도구는 제거했습니다.
실행 시 별도 JavaScript/JSON 파일이나 CDN, Node.js는 필요하지 않습니다. 기존 서버에서 `/demo_v2.html`을 열면 됩니다.
API 요청·응답은 실제 서버를 사용하며 JSON 예시 파일을 읽어 화면을 구동하지 않습니다.
`scripts/demo_v2/package.json`과 `package-lock.json`은 선택적인 Playwright 자동 테스트용이며 화면 실행에는 사용하지 않습니다.

## 검증 범위와 결과

Python 계약·노드 테스트 52개 통과:

```sh
PYTHONSAFEPATH=1 PYTHONPATH=src:. .venv/bin/python -m unittest \
  app.test.test_workflow_hitl_v2 \
  app.test.test_workflow_hitl_agent_contract \
  app.test.test_workflow_hitl_actions \
  app.test.test_candidate_selection_v2 \
  app.test.test_tool_inputs_v2 \
  app.test.test_plan_review_v2 -q
```

Chromium 브라우저 테스트:

- mock 생성 후보의 실제 응답 ID로 선택하고 v2 Tool input으로 전환.
- 실제 Tool Registry의 인자·기본값·사전 입력값으로 폼 구성.
- 필수값·타입·enum 오류와 빈 거절 사유가 HTTP 요청으로 전송되고 실제 API 검증에서 422로 거부되는지 확인.
- REST 및 POST SSE의 실패 응답을 화면에 출력하고 잘못 입력한 값도 유지하는지 확인.
- 선택 인자 생략 후 실제 컴파일러가 기본값을 반영한 계획으로 전환.
- 실제 계획 노드에서 승인/거절 해석, 승인한 workflow와 표시한 workflow 일치.
- REST/POST SSE 두 전송 방식으로 선택→입력→계획 전환.
- 후보 재생성/거절의 intent가 실제 선택 노드에 도달하는지 확인.
- 동일 sequence snapshot의 작성값 보존, 409 재조회 후 작성값 보존.
- SSE 파싱을 통한 채널 구분·message_id 중복 제거·제출 폼 닫기.
- boolean false, 숫자 0, 명시적 null, 빈 문자열의 실제 DOM 수집 결과 확인.
- JavaScript page error 없음.

브라우저 테스트의 HTTP 저장/조회/SSE 응답은 Playwright에서 대체합니다.
Python bridge는 실제 후보 선택·입력 수집·입력 반영·계획 승인 노드, 실제 API resume 검증,
실제 Registry/컴파일러와 메모리 checkpoint를 사용합니다.
재생성/거절은 해당 노드의 해석까지 확인하며 이후 LLM 재생성·의도 라우팅은 실행하지 않습니다.
실제 PostgreSQL, 인증, run-worker, LLM, Redis, 원격 executor 및 시스템 동시 부하는 이번 검증에 포함하지 않습니다.

## 브라우저 테스트 재현

저장소 루트에서 Python 프로젝트 의존성과 Node.js를 준비한 뒤 실행합니다.
`PYTHON`을 생략하면 저장소의 `.venv/bin/python`을 사용합니다.

```sh
npm ci --prefix scripts/demo_v2
# Chromium이 없는 환경에서 설치
(cd scripts/demo_v2 && npx playwright install chromium)
PYTHON="$PWD/.venv/bin/python" npm run --prefix scripts/demo_v2 test
```

기존 Chromium 실행 파일을 사용하려면 `CHROMIUM_PATH`를 지정합니다.
스크린샷 경로는 기본 `/tmp/dtest-demo-v2`이고 `DEMO_TEST_OUTPUT`으로 바꿀 수 있습니다.

실제 서비스 확인은 서버 실행 후 `/demo_v2.html`에서 새 세션으로 시작합니다.
새 생성 후보의 선택·Tool input 제출·계획 검토를 확인하고, 실제 실행 승인까지 확인하려면
기존 DB/Worker/Executor 개발 환경이 준비되어 있어야 합니다.
