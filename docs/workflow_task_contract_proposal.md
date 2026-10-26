# Task 중심 Workflow API 계약 제안

상태: 단계적 구현을 위한 설계안. Task snapshot/Task SSE/재개 대상/Task 보존/공개 메시지 이벤트는 반영했다.
별도 interaction revision 저장, 공개 token delta, plan/step 이벤트, fencing/drain/부하 검증은 후속 범위다.
아래 차이 분석은 검토 당시 기준이며 현재 동작은 `workflow_run_api.md`를 따른다. PM 예시는 고정 계약이 아닌 참고안으로 해석한다.
사용자 방향인 **Task = 전체 요청, Run = 요청 안의 개별 실행 구간**을 우선한다.

## 1. 식별자와 수명

| 식별자 | 의미 | 생성 주체/수명 |
| --- | --- | --- |
| session_id | 대화방 | 서버 / 여러 Task 포함 |
| task_id | 한 사용자 요청의 전체 처리 | 서버 / 입력 대기·재개·자동 복구·결과까지 고정 |
| run_id | 한 번의 실행 구간 | 서버 / 최초 제출·사용자 재개 시 새로 생성 |
| interaction_id | 사용자가 답할 질문·승인·폼 | 서버 / 하나의 대기 항목 식별 |
| revision | 해당 대기 항목의 버전 | 서버 / 같은 항목의 내용 변경 시 증가 |
| message_id | 표시 메시지 | 서버 / delta와 최종 메시지의 결합 및 중복 제거 |
| plan_id, step_id | 실행 계획과 그 단계 | 서버 / Run ID와 별도 관리 |
| Idempotency-Key | 클라이언트의 한 번의 제출 의도 | FE / 네트워크 재전송 동안 동일 |

PM의 고정 run_id 요구가 해결하려는 FE 동기화는 task_id가 담당한다.
Run 하나가 도구 하나나 노드 하나를 뜻하지 않는다. 한 Run에 여러 노드·도구 실행이 포함된다.
자동 재시도는 동일 run_id에서 attempt_count를 증가시키고, 사용자 재개는 새 Run을 만든다.
체크포인트 ID는 서버 내부 복구용이며 FE가 다음 실행에 복사할 ID가 아니다.

첫 202 응답부터 task_id와 run_id 모두 존재해야 한다. 현재 큐 등록 구현도 이미 두 행을
생성하므로 run_id=null을 반환할 이유가 없다. task_id도 처리 도중 null로 바꾸지 않는다.

## 2. API와 FE의 기준 리소스

| API | 역할 |
| --- | --- |
| POST /api/v1/workflow_run | 신규 요청·재개를 수락하고 Task snapshot 반환 (202) |
| POST /api/v1/workflow_run/stream | 같은 수락 처리를 한 뒤 Task SSE 구독 시작 |
| GET /api/v1/tasks/{task_id} | 새로고침 시 화면 복원용 Task snapshot |
| GET /api/v1/tasks/{task_id}/stream | Run이 바뀌어도 이어지는 FE 기본 SSE |
| GET /api/v1/tasks/{task_id}/runs | 실행 이력; 페이지네이션 추가 |
| GET /api/v1/workflow_run/{run_id} | 특정 Run의 상세/진단 |
| GET /api/v1/workflow_run/{run_id}/stream | 특정 Run만 보는 보조 스트림 |
| GET /api/v1/sessions/{session_id}/tasks | 대화방의 요청 카드 목록 |
| POST /api/v1/tasks/{task_id}/cancel | 전체 요청 취소 |

인증은 X-User-ID이며, 소유권 검사와 POST 멱등성은 유지한다.
Task snapshot은 최초 제출, 재개 수락, GET 조회, SSE task.snapshot에서 동일한 스키마를 쓴다.
Run 조회와 Task 조회 응답은 서로 다른 타입으로 문서화한다.
202는 결과 완료가 아닌 큐 수락이며, 이후 GET/SSE에서 waiting_input이나 최종 결과를 받는다.

## 3. Task snapshot 예시

아래 T/R/I 등은 설명용 ID 표기이며 실제 값은 UUID다.

```json
{
  "schema_version": 1,
  "task_id": "T",
  "session_id": "S",
  "current_run_id": "R1",
  "main_model_name": "default",
  "status": "waiting_input",
  "phase": "plan_review",
  "last_event_sequence": 18,
  "input_required": true,
  "task_finished": false,
  "current_run": {
    "run_id": "R1",
    "status": "interrupted",
    "attempt_count": 1
  },
  "interactions": [
    {
      "interaction_id": "I1",
      "revision": 1,
      "run_id": "R1",
      "status": "open",
      "kind": "plan_review",
      "payload_version": 1,
      "summary": "계획과 입력값을 확인해 주세요.",
      "payload": {"plans": [], "notices": []},
      "actions": [
        {"name": "approve_plan", "label": "승인", "input_schema": {"type": "object", "properties": {}}}
      ],
      "resume_target": {
        "task_id": "T",
        "run_id": "R1",
        "interaction_id": "I1",
        "revision": 1
      }
    }
  ],
  "capabilities": {"can_cancel": true, "can_resume": true},
  "result": null,
  "failure": null,
  "recovery": {"status": "none", "next_attempt_at": null, "user_action_required": false}
}
```

created_at/updated_at/started_at/completed_at는 Task 기준으로 최상위에 둔다.
Run의 시각·attempt_count는 current_run 또는 Run 상세에 둔다.
result에는 최종 메시지·보고서·산출물 참조를 제공한다. 긴 대화 전체와 모든 Run 로그를
snapshot에 넣지 않고 페이지네이션 조회로 분리한다.
main_model_name은 Task에 선택값을 보존하고 재개에서도 상속한다. 실제 모델 선택 기능이
구현되기 전에는 default만 지원한다. 배포로 기본 모델이 바뀌어도 진행 중 Task의 실제
모델 설정을 유지할지 모델 설정 버전을 고정할지 별도 정책으로 확정한다.

Task status는 pending/running/waiting_input/success/error/timeout/canceled를 사용한다.
Executor 대기는 phase=executor_wait로 구분하고 input_required=false다.
run.interrupted는 해당 실행 구간의 정지이지 전체 요청 완료가 아니다.
스트림 연결 종료와 Task 완료를 혼동하지 않도록 `task_finished`와 별도의 `stream.closed`
이벤트를 사용한다. 현재 run_finished/stream_finished는 호환 기간 후 명칭을 정리한다.
자동 복구 중이라는 사실과 사용자 조치 필요 여부도 recovery 내부에서 분리한다.

## 4. 재개와 동시 제출

FE는 interaction.resume_target의 값을 그대로 복사하고 답변만 추가한다.
새 run_id를 FE가 미리 생성하지 않는다. 요청의 run_id는 답변 대상인 기존 Run이다.
서버는 검증/수락 후 새 Run을 만들고 응답의 current_run_id를 R2로 바꾼다.

```json
{
  "task_id": "T",
  "run_id": "R1",
  "command": {
    "resume": {
      "action": "answer_clarification",
      "interaction_id": "I1",
      "revision": 1,
      "feedback": "전체 수치 컬럼을 대상으로 분석해줘."
    }
  }
}
```

복합 폼은 feedback에 JSON을 문자열로 넣지 않고 payload 객체를 사용한다.
각 action의 input_schema와 서버의 Pydantic 입력 모델이 일치해야 한다.
현재 내부 approve/edit/respond/reject 변환 앞에 명시적인 action adapter를 둔다.
`answer_clarification`, `approve_plan`, `revise_plan` 등의 외부 동작 이름을 서버가 제공하고
FE는 문자열을 임의로 추측하지 않는다. 현재 코드가 이 PM action 이름들을 지원한다는
뜻은 아니다.

Task/interaction 잠금 아래 다음 조건을 한 트랜잭션으로 검사하고 기록한다.

1. X-User-ID 소유권과 task/run/interaction 관계 일치.
2. interaction이 open이고 revision 일치, Task가 재개 가능한 상태.
3. action 허용 여부 및 답변 스키마 검증.
4. interaction을 submitted로 전환하고 새 Run/큐 이벤트를 함께 기록.
5. 같은 멱등 키 재전송에는 수락 결과를 재사용. 다른 제출과 충돌하면 409.

409 응답은 `INTERACTION_STALE`, `ALREADY_SUBMITTED` 등 기계 판별 code와 task 조회 링크를
준다. FE는 작성 답변을 보존하고 최신 snapshot을 조회하여 폼을 갱신한다.

resume_token은 초기 버전에서는 추가하지 않는다. 위 식별자·revision·상태 검증과 멱등 키가
서로 다른 목적을 이미 담당한다. 향후 불투명 continuation token이 필요하면 서버 발급으로
추가하되 run_id나 인증 토큰을 대체하는 값으로 사용하지 않는다.

## 5. SSE 계약과 동적 화면

공통 envelope:

```json
{
  "schema_version": 1,
  "event_id": "E",
  "type": "interaction.opened",
  "sequence": 18,
  "session_id": "S",
  "task_id": "T",
  "run_id": "R1",
  "occurred_at": "2026-10-02T00:00:00Z",
  "data": {}
}
```

sequence는 Run별로 초기화하지 않고 Task 전체에서 단조 증가한다. 현재 DB의
UNIQUE(task_id, sequence)를 활용한다. event_id에는 DB의 task_event_id를 사용할 수 있다.
SSE id는 sequence이며 FE는 task_id+sequence로 중복 이벤트를 제거한다.

| 이벤트 | FE 처리 |
| --- | --- |
| task.snapshot / task.status_changed | 요청 카드의 상태와 버튼 갱신 |
| message.delta / message.completed | message_id로 합쳐 채팅 메시지 표시 |
| interaction.opened / updated / submitted / resolved | 질문·승인·입력 폼 표시 및 비활성화 |
| plan.updated / step.status_changed | 우측 계획·도구·파라미터·진행 패널 갱신 |
| task.completed / task.failed / task.canceled | 전체 요청의 최종 상태와 결과 표시 |
| stream.closed | 해당 연결의 종료 사유 전달; 업무 완료와 별개 |

message data는 role, channel, content를 포함한다. channel=commentary는 진행 안내,
channel=final은 사용자에게 전달할 답변이다. 이 구분은 대상 사용자에게 공개할 메시지에만
적용하며 내부 모델 토큰/라우터 출력/도구 인수를 그대로 commentary로 노출하지 않는다.
현재 llm.token.delta에는 공개 채널 분류가 없으므로 그대로 PM의 message.delta로 이름만
바꾸는 방식은 피한다. 명시적인 UI 메시지 생성·분류 경계를 둔다.

interaction은 kind+payload_version으로 구분되는 Pydantic union으로 관리한다.
초기 kind 후보는 planning_question, data_selection, plan_review, input_form이다.
각 payload에는 기본값, 입력 schema, required/editable, 선택지, 검증 오류를 넣는다.
plan_review는 plan_id/plan_revision, inputs, steps, dependencies, execution 설정과
허용 범위, outputs를 가진다. 실제 현재 도메인 모델에서 제공할 수 있는 필드부터 adapter로
변환하며, PM 예시의 전체 plan 구조를 그대로 구현되었다고 표시하지 않는다.
FE는 미지원 kind에서 summary를 표시하고 원시 기술 데이터 대신 안전한 fallback UI를 쓴다.
서버는 editable과 허용 범위를 재검증한다. 클라이언트 폼의 제약만 믿지 않는다.

## 6. 새로고침·재접속·복수 탭

- 기본 구독은 Task 스트림이다. 사용자 재개로 Run이 바뀌어도 같은 구독을 유지한다.
- waiting_input에서는 입력 폼을 표시하고 Task 스트림을 유지한다. 유휴 연결 정책으로 닫을
  경우 stream.closed에 reason=waiting_input을 넣고 제출/화면 복귀 시 재구독한다.
- GET snapshot의 last_event_sequence=N과 상태는 같은 일관된 DB snapshot에서 읽는다.
  FE는 상태를 적용한 뒤 N 다음부터 이벤트를 구독하여 조회/구독 사이의 변경을 놓치지 않는다.
- cursor 없는 최초 SSE 연결은 snapshot at N을 먼저 보내고 N 이후 이벤트를 전송한다.
- Last-Event-ID가 있으면 해당 cursor 이후 이벤트를 순서대로 재생한다. 최신 snapshot을
  먼저 적용시킨 다음 과거 status 이벤트로 덮어쓰는 순서는 금지한다.
- cursor 보관 기한이 끝났거나 현재 최대값보다 크면 명시적으로 재동기화를 요구한다.
  FE는 snapshot과 필요한 메시지 이력을 다시 조회한다. 조용히 무한 대기하지 않는다.
- optimistic user message는 client_request_id/멱등 키로 서버 message_id와 연결한다.
- 다른 탭에서 제출하면 interaction.submitted 이벤트로 같은 폼을 비활성화한다.
- 연결 끊김은 Task 취소가 아니다. 재전송은 같은 멱등 키로 진행한다.

## 7. 현재 구현과의 차이

| 현재 코드 | 판단 / 후속 변경 |
| --- | --- |
| RunService가 Task와 최초/재개 Run을 분리 | 유지 |
| task_events의 Task 단위 sequence, DB replay | 유지하며 envelope 표준화 |
| workflow_run POST SSE는 Run 단위 | FE 기본 경로는 Task 스트림으로 변경 |
| TaskResource가 lock_owner/lease 중심 | 별도 운영 상세와 FE snapshot 분리 |
| RunService가 FAQ/file_lookup Task를 삭제 | 전체 요청 식별 원칙에 맞게 Task 보존; 이벤트 cascade 삭제도 방지 |
| interactions가 Run interrupt에서 파생되고 revision=1 | 불변 항목에는 충분. 수정/제출 상태를 다루려면 영속 interaction 필요 |
| RunResource에 raw interrupt, 별도 interactions가 공존 | 공개 응답은 정규화된 interactions 하나로 통일 |
| 기존 tasks/{id}/resume은 checkpoint ID를 대상에 사용 | 현재 open interaction 기준의 동일 재개 검증 서비스로 통합 |
| 현재 latest Run 검사가 session 기준 | Task별 현재 Run/interaction 기준으로 변경 |
| 기본 모델 필드 metadata 보존 | Task snapshot에서 모델 선택값을 일관되게 노출 |

Task 보존 변경 시 FAQ의 next_user_request 대기를 전체 요청의 완료와 분리해야 한다.
새 사용자 질문은 같은 Session의 새 Task이며, 예전 Task의 열린 승인 폼으로 취급하지 않는다.
Session의 동시 활성 Task 허용 정책과 기존 잠금/unique 제약도 함께 확인한다.

## 8. 배포·복구와 부하 검증

이미 DB queue, SKIP LOCKED claim, heartbeat/lease, reconciler 재시도, DB 이벤트 보존이 있다.
이는 복구 기반이지 무중단·중복 실행 방지 보장이 검증되었다는 뜻은 아니다.

특히 현재 heartbeat 실패 시 heartbeat 루프만 종료하고 실행 자체를 확실히 중단시키는
경로가 보이지 않는다. 실행 완료 기록에도 claim 당시 lock_token을 검사하지 않는다.
lease가 넘어간 뒤 이전 Worker가 결과를 쓰는 상황을 막도록 Task/Run 상태 기록, 메시지,
이벤트 및 외부 작업 경계에 fencing/멱등성 정책이 필요하다. DB fencing만으로 외부 효과가
중복되지 않는다고 보장할 수 없으므로 Executor의 실행 키와 결과 조회도 검증한다.

| 시나리오 | 수용 조건 |
| --- | --- |
| 큐 수락 직후 응답 유실 | 같은 멱등 키 재전송 시 Task/Run 중복 없음 |
| 실행 중 Worker 강제 종료 | lease 만료 후 정해진 복구 정책 적용, 오래된 Worker 쓰기 차단 |
| 입력 대기 중 재배포 | interaction/답변 폼 복원, 모델 재실행 없이 대기 유지 |
| Executor 접수 직후 API/Worker 종료 | 중복 외부 실행 없이 기존 실행 결과 추적 |
| SSE Pod 교체 | 다른 Pod에서 cursor 재생, 최종 메시지·interaction 복원 |
| 두 탭 동시 승인 | 재개 Run 한 개만 수락, 다른 제출은 일관된 충돌 응답 |
| rolling deployment | 신규 claim 중단·readiness 전환·진행 작업 drain 또는 복구 정책 명시 |
| 이벤트 버전이 다른 Pod 혼재 | 호환 envelope/schema, DB migration의 선후 배포 순서 검증 |

사용 중 재배포는 SSE 재연결까지 포함한 사용자 경험과 Worker 복구 조건을 통과한 뒤 지원
범위로 명시한다. 현재 router lifespan 종료는 Worker 취소 방식이므로 graceful drain을
별도로 설계해야 한다. deploy YAML에는 readinessProbe가 있으나 이것만으로 충분하지 않다.

부하는 신규 제출뿐 아니라 동시 Task 구독 수, token event 쓰기량, DB pool 대기,
Task sequence row 경합, p95 큐 대기·첫 이벤트·재개 시간, replay 지연을 함께 측정한다.
각 구독이 polling마다 상태/이벤트 쿼리를 수행하므로 구독자 수에 따른 DB 부하가 증가한다.
Redis 알림/fanout 최적화는 측정 후 적용하며 PostgreSQL replay를 기준으로 유지한다.

## 9. 구현 묶음과 완료 조건

이는 담당자 가용성과 통합 환경을 확인하기 전의 순서 제안이며 날짜 확약이 아니다.

1. **계약/식별자 정리**: Task snapshot, Run 의미, action registry, 오류 code 확정.
   완료 조건: FE와 같은 예시로 신규/대기/재개/완료/오류 화면을 해석 가능.
2. **Task 중심 조회·재개**: Task 보존, snapshot, interaction 저장·중복 제출 검증, 기존 resume
   경로 통합. 완료 조건: 새로고침/두 탭/멱등 재전송 통합 테스트 통과.
3. **이벤트·메시지·폼**: envelope, Task SSE, 공개 메시지 분류, payload 모델/adapter.
   완료 조건: FE가 Run 변경 없이 승인부터 최종 보고서까지 하나의 Task로 표시.
4. **복구·배포 안전성**: fencing, 외부 멱등성, claim 중단/drain, 강제 종료·배포 테스트.
   완료 조건: 위 장애 시나리오별 결과 및 운영 절차 확보.
5. **부하·FE 통합 회귀**: 목표 동시 사용자/Task/SSE 규모를 정하고 측정·병목 수정.
   완료 조건: 합의한 지연/오류율/복구 시간 기준과 실측 보고서.

Agent/API 담당은 1~3의 계약·영속화·adapter를, FE 담당은 snapshot reducer/동적 폼/재접속을,
인프라·QA 담당은 4~5의 배포/강제 종료/부하 시나리오를 맡아 병행하는 구성이 적절하다.
실제 사람 이름이나 종료일은 가용 인력과 환경을 확인한 뒤 정한다. PM 문서의 10/31은
목표 일정으로만 기록하고 위 완료 조건을 제외한 상태에서 달성됐다고 간주하지 않는다.
