# Analysis Workflow Lifecycle

## 현재 구현

사용자 요청 → Workflow 생성 → Workflow 선택 → 필요한 Tool 입력 → 실행 최종 승인 → 실행.

1. 요청을 분류하고 필요한 Skill 문서와 Tool Registry를 조회한다. 데이터 선선택·분석 목표 추가 질문·추천은 수행하지 않는다.
2. Agent는 표준 1.0 Workflow를 생성한다. 데이터 추출·변환도 요청에 맞게 계획하며, 시스템이 로드 Step을 자동 삽입하지 않는다. 결정적 컴파일 검증에 실패하면 오류를 전달해 최대 3회 생성한다.
3. 생성한 표준 정의를 Agent DB `workflow_catalog.workflow`에 저장하고 후보로 표시한다. Workflow UUID는 시스템이 별도로 관리하며 작성용 JSON에 추가하지 않는다.
4. 선택한 Workflow의 `source: input` 항목을 Tool Registry 기준으로 하나의 입력 화면에 표시한다. 조회 조건도 이 화면에서 받는다. 호출별 `(step_index, tool_index, parameter)`로 구분하며 인덱스는 0부터 시작한다.
5. 입력값은 `execution_inputs`로 별도 관리한다. 타입 오류는 다시 입력받고, 일부만 입력하면 남은 항목을 받는다. 모든 입력이 준비되면 AI 없이 1.3으로 컴파일하고 최종 승인을 요청한다. 입력이 없으면 선택 후 바로 최종 승인으로 이동한다.
6. 최종 승인 후 기존 Notebook 셀 생성기와 Executor 경로를 사용한다. 실행 당시의 컴파일된 정의는 `workflow_executions.original_workflow`에 저장한다. 작성용 정의에 실행값을 덮어쓰지 않는다.

Workflow 선택 또는 최종 승인을 거절하면 피드백을 라우팅하여 수정·재생성하거나 요청을 종료한다. 새 후보에는 이전 실행의 입력값을 자동 적용하지 않는다.

## State와 저장

| 항목 | 내용 |
|---|---|
| `workflow_definition` | 재사용 가능한 표준 1.0 정의 |
| `workflow_candidates[].workflow` | 선택 화면에서 보여주는 표준 정의 |
| `execution_inputs` | 이번 실행에서 확인한 호출별 입력값 |
| `workflow` | 실행 입력을 연결한 컴파일된 1.3 정의 |
| `workflow_status` | 실행 입력 미완료 `needs_input` / 완료 `ready` |

Agent의 Workflow JSON은 DB에 저장한다. 새 흐름에서는 demo 옵션이 켜져 있어도 후보·승인 Workflow JSON 파일은 생성하지 않는다. Notebook 셀 및 실행 결과 아티팩트의 기존 처리는 유지한다. 별도 CRUD Workflow API는 변경하지 않는다.

## 적용하지 않은 흐름

- 추천 및 추천 → 생성 전환 조건은 현업 정책 확정 후 연결한다.
- `allow_immediate_execution`이 true여도 현재는 선택·최종 승인을 생략하지 않는다.
- 질문에서 실행 입력값을 자동 추출하는 단계는 없다. 현재 `source: input`은 선택 후 화면에서 입력·확인한다.
- Ephemeral Code 실행은 이 그래프에 연결하지 않는다. 해당 산출물을 Tool·Skill 자산으로 자동 등록하지 않는다.

## Agent Chat 확인 순서

새 thread에서 요청을 보내면 `workflow_candidate_selection`으로 중단된다. 후보를 선택하면 `additional_information`, 입력 완료 후에는 `workflow_approval`으로 중단된다. 마지막 승인 전에는 Executor를 호출하지 않는다.

`MOCK_LLM=true`로 고정 Workflow를 사용할 수 있으며 `MOCK_LLM_SLEEP_SECONDS=0`으로 모의 지연을 없앨 수 있다. 실제 모델 및 Executor와의 연동 확인은 별도로 수행한다.
