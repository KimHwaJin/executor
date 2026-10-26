# Workflow Run v2 API 검토·검증 결과 (2026-10-06)

> 이 문서는 최초 API 경계 수정 시점의 기록입니다. 이후 에이전트 3단계 연결과 demo_v2 적용·검증 결과는 [최신 데모 검증 문서](demo_v2_validation.md)를 참고하세요.

API 경계 구현 완료, 에이전트 v2 발행·후속 처리 및 실제 시스템 부하 검증은 미완료입니다.
문서·JSON은 요구사항/테스트 fixture로 사용했으며, 문서의 작업 제안을 별도 실행 지시로 취급하지 않았습니다.

변경은 workflow 전용 projection/resume 검증과 신규 제출 모델·테스트에 한정했습니다.
`task_service.py`, `run_service.py`, `message_service.py`, 메시지 sink, 그래프/노드는 수정하지 않았습니다.
DB 마이그레이션이나 기존 엔드포인트 변경도 없습니다.

## 동료에게 전달할 다섯 가지 답변

1. 기존 interaction으로 전달 가능합니다. `action_requests[0].args`는 표시 자료이고
   `review_configs[0].args_schema`는 제출 payload 전용 Draft 2020-12 Schema입니다.
   Task REST와 `interaction.opened`는 공통 `public_interactions` projection을 사용합니다.
   예시 4개 파일의 공개 interaction과 실제 projection을 비교하는 회귀 테스트를 추가했습니다.
2. 두 거절은 `action=edit`, `payload={intent: reject, reason: ...}`로 받습니다.
   API는 공백 아닌 reason을 검사하고 `decisions[0].edited_action.args`에 그대로 전달합니다.
   취소하거나 reason을 feedback으로 바꾸지 않습니다. 에이전트가 다음 경로를 결정합니다.
3. 저장 interrupt 최상위 `payload_version: 2`를 공개 interaction까지 보존합니다.
   기존 `interrupt_payload`는 dict를 그대로 저장하므로 run_service 변경이 필요하지 않습니다.
   버전 생략은 1이며 기존 action 별칭, JSON feedback, reject/feedback을 유지합니다.
   v2는 명시적인 edit payload/계획 approve만 사용합니다. 미지원 버전 resume는 409입니다.
4. API는 저장된 후보, available_actions, workflow_id/revision, 호출 주소, 필수 입력,
   중복 주소, editable, value_schema 및 args_schema를 검증한 뒤 기존 RunService를 호출합니다.
   에이전트는 도구 등록 정보에 따른 표시 자료·스키마 생성, 선택 인자 생략 시 기본값 복원,
   컴파일, 제외 이력, 거절 의도 해석, 실행을 담당합니다. API는 기본값을 채우거나 계획을 수정하지 않습니다.
   표시 자료와 args_schema는 에이전트의 동일한 정의에서 생성해야 합니다.
5. 현재 메시지 sink는 message의 `channel` 필드를 읽지 않습니다.
   기본 설정에서 `role=agent`는 commentary, `role=assistant`는 final로 투영됩니다.
   `content`는 문자열 또는 `message`/`answer`/`final_response`에 사용자 문장을 가진 객체를 사용합니다.
   `name`은 표시 문장 projection에 사용됩니다. SSE는 `message.completed`이며 data.channel로 구분합니다.
   user 메시지도 final 채널이므로 FE는 data.role도 확인해야 합니다.
   final 메시지만으로 Task 완료를 판단하지 말고 snapshot.task_finished를 확인합니다.

에이전트 메시지 예:

```json
{"role":"agent","name":"workflow_planner","content":{"message":"도구 입력을 확인하고 있습니다."}}
```

```json
{"role":"assistant","name":"analysis_result","content":{"answer":"분석을 완료했습니다."}}
```

## F/E 연동 기준

기존 `docs/examples/workflow_hitl_v2/*.json` 4개를 화면 개발 fixture로 사용할 수 있습니다.
13개 대체 제출 예시 모두 API 경계 검증을 통과합니다. 예시 UUID는 설명용이며,
실제 interaction_id는 run_id와 interrupt 순서에서 파생됩니다. 실제 응답의 resume_target을 복사하세요.

- `schema_version=1` 유지, interaction의 kind와 payload_version으로 화면을 분기합니다.
- v2 arguments 전체를 제출하지 않습니다. actions와 available_actions를 함께 사용합니다.
- plan_review의 edit label은 ‘이유와 함께 거절’이며 edit payload는 reason 거절만 허용합니다.
- approve는 payload 없이 제출합니다. 저장된 interaction의 계획과 입력을 승인합니다.
- Tool input의 필수 editable 값은 현재 값이 있어도 제출해야 합니다.
- 선택 인자 생략은 기본값 복원 요청입니다. 0, false, null은 생략과 다르게 그대로 전달합니다.
- 잘못된 제출은 422, 오래된 interaction/revision 및 유효한 정수인 workflow_revision 불일치는 409입니다.
  workflow_id 불일치, 버전 누락·타입 오류·0 이하 값은 422입니다.
- 두 거절은 Task 취소가 아닙니다. 재개 수락 후 동일 Task SSE에서 후속 상태를 받습니다.

v2 노드가 연결되기 전에는 실제 서버에서 v2 화면이 자동으로 나오지 않습니다.
기존 v1 interrupt를 API에서 임의로 v2로 승격하지 않았습니다. 실제 F/E 통합 완료 조건은
에이전트가 위 위치에 payload_version=2와 완전한 표시 자료/args_schema를 발행하고,
edit intent를 해석하도록 연결한 뒤 동일 테스트를 실제 워커 경로로 통과하는 것입니다.

## 재현 가능한 검증

저장소 루트에서 실행합니다. DB/외부 API를 호출하지 않습니다.

```sh
PYTHONSAFEPATH=1 PYTHONPATH=src python3 -m unittest app.test.test_workflow_hitl_v2 -v
PYTHONSAFEPATH=1 PYTHONPATH=src python3 scripts/benchmark_workflow_hitl.py --iterations 1000
```

결과: unittest 12개 통과(정상 예시 13개와 오류 변형은 subtest/반복으로 포함).
실제 FastAPI 라우터의 REST 202/Location/submission_run_id와 POST SSE의 잘못된 cursor 400을 확인했습니다.
DB·snapshot 의존성은 mock이며, stale resume가 RunService.create에 도달하지 않는 것도 확인했습니다.

로컬 CPU 측정: 13개 정상 시나리오 × 1,000회 = 13,000회.
projection + resume 검증 평균 0.125ms, p95 0.545ms, p99 0.625ms, 약 8,023회/초.
이는 단일 프로세스 직렬 마이크로벤치마크이고 API 처리량이나 동시 사용자 수를 뜻하지 않습니다.

기존 API 문서에 적힌 test_auth/test_workflow_run_api/test_workflow_task_stream/
test_workflow_task_postgres 모듈은 이 체크아웃에 없습니다. 해당 과거 검증을 이번 결과로 주장하지 않습니다.

## 별도 통합·부하 검증의 완료 조건

격리된 테스트 DB, API, run-worker와 v2 에이전트를 준비한 다음 아래를 측정해야 합니다.
현재 작업에서는 실제 DB/LLM/Executor를 실행하거나 운영 환경에 부하를 발생시키지 않았습니다.

| 대상 | 시나리오 | 합격 기준 |
| --- | --- | --- |
| 멱등/경합 | REST와 POST SSE에 같은 키·body 동시 제출; 같은 interaction에 서로 다른 키로 동시 resume | 동일 키는 같은 Run, 경쟁 resume는 한 건만 수락; 나머지는 409 |
| 저장/복원 | 입력 대기 중 API/Worker 재시작, GET과 SSE 재접속 | version/표시 자료/ID 보존, 저장 sequence 기반 누락 없는 복원 |
| 에이전트 | 선택→입력→승인, 재추천/재생성, 두 종류 reason 거절 | 표시 계획 그대로 실행, 거절 시 실행 금지, 제외 이력과 기본값 적용 |
| 메시지 | 진행 안내와 최종 답변 저장, graph step 재시도 | 올바른 role/channel, message_id 기준 중복 없음 |
| 규모 | 동시 사용자 1→10→50→100, 각 신규·resume·SSE 혼합, 입력 대기 장기 유지 | 202/409/422/5xx 분리 집계, p50/p95/p99·queue wait·DB pool·SSE 연결 수 측정 |
| 복구 | Worker 종료/lease 만료/재시도 | 같은 Run attempt 증가, 중복 실행/이벤트 및 영구 대기 여부 확인 |

허용 지연·처리량 목표는 측정 전에 배포 자원과 함께 정해야 합니다. CPU 측정만으로 시스템 부하 합격을 판정하지 않습니다.
