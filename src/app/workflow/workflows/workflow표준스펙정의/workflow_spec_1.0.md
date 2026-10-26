# Workflow 표준 규격 1.0

Workflow는 사용자 요청을 수행하기 위한 Skill·Tool 구성, 실행 순서 및 인자 연결을 정의하는 JSON 문서다. 사람이 작성하거나 Agent가 생성할 수 있으며, 저장·추천·재사용의 대상이다.

공식 구조 검증 규격: [workflow_schema_1.0.json](workflow_schema_1.0.json). Skill/Tool 등록 여부와 출력 참조의 유효성 등은 별도 의미 검증 대상이다.

## 1. 기본 구조

```text
workflow_version
workflow
├─ actor
│  ├─ type
│  └─ id
├─ name
├─ user_request
├─ execution_mode
├─ allow_immediate_execution
└─ steps[]
   ├─ skill
   └─ tools[]
      ├─ tool
      ├─ execution
      └─ arguments
```

## 2. 필드 정의

| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| `workflow_version` | string | 예 | 규격 버전. `"1.0"` |
| `workflow` | object | 예 | 워크플로우 정의 |
| `workflow.actor` | object | 예 | 작성 주체 |
| `actor.type` | string | 예 | `USER` 또는 `AGENT` |
| `actor.id` | string | 예 | 작성한 사용자 또는 Agent의 식별자 |
| `workflow.name` | string | 예 | 워크플로우 이름 |
| `workflow.user_request` | string | 예 | 워크플로우가 수행할 사용자 요청 |
| `workflow.execution_mode` | string | 예 | `static` 또는 `adaptive` |
| `workflow.allow_immediate_execution` | boolean | 예 | 조건 충족 시 사전 확인·입력 화면을 생략하고 실행할 수 있는지 여부 |
| `workflow.steps` | array | 예 | 순서대로 수행할 Step 목록. 최소 1개 |
| `steps[].skill` | string | 예 | 등록된 Skill 이름 |
| `steps[].tools` | array | 예 | 해당 Step에서 호출할 Tool 목록. 최소 1개 |
| `tools[].tool` | string | 예 | 해당 Skill에 속하는 등록된 Tool 이름 |
| `tools[].execution` | string | 예 | `always` 또는 `conditional` |
| `tools[].arguments` | object | 아니요 | Tool 파라미터 이름별 값 또는 참조. 기본값 `{}` |

## 3. 실행 규칙

워크플로우를 먼저 정의한 뒤, 데이터 조회 조건과 Tool 인자를 하나의 입력 화면에서 받는다. 데이터 조회 조건도 해당 조회 Tool의 `source: input` 인자로 표시하며, 실제 값은 실행별로 관리한다. 로드된 데이터는 후속 Tool에서 `tool_output`으로 참조한다.

- Step과 Tool은 배열에 작성된 순서대로 실행한다. 구성은 사용자 요청에 따라 정한다.
- `step_index`와 `tool_index`는 **0부터 시작**한다. `steps[0].tools[1]`은 첫 번째 Step의 두 번째 Tool이다.
- `always`는 항상 실행하며, `conditional`은 조건 판단에 따라 실행하거나 생략한다.
- `static`은 모든 Tool 호출이 `always`인 워크플로우다.
- `adaptive`는 `conditional` Tool 호출이 하나 이상 있는 워크플로우다.
- Tool의 실행 방식과 조건은 등록된 Skill 규칙과 일치해야 한다. 조건 내용과 판단에 사용할 Tool은 Skill 정의를 따른다.
- `allow_immediate_execution`은 즉각 실행을 허용하는 표시다. 실제 실행 여부는 추천 기준, 필요한 값의 준비 여부 등 서비스 정책에 따라 결정한다.

## 4. tools[].arguments

`arguments`의 키는 `data`, `columns`와 같은 함수 파라미터 이름이고, 각 파라미터 안의 `source`는 값의 출처를 나타내며, `literal`, `input`, `tool_output` 중 하나를 사용한다.

| source | 의미 | 작성 예시 | 규칙 |
|---|---|---|---|
| `literal` | 고정값 | `{"source":"literal","value":"artifacts/metrics"}` | `value` 필수. 해당 파라미터에 맞는 JSON 값 사용 |
| `input` | 사용자에게 받을 입력 | `{"source":"input"}` | 타입·설명은 Tool 정의를 따름. 호출별 개별 입력 |
| `tool_output` | 앞선 Tool 호출의 출력 | `{"source":"tool_output","step_index":0,"tool_index":1,"output":"data"}` | 두 index와 등록된 출력 이름 `output` 필수 |

- `input`은 `(step_index, tool_index, 파라미터 이름)`으로 구분한다. 같은 파라미터 이름이어도 다른 호출의 입력과 자동 공유하지 않는다.
- `tool_output`은 현재 호출보다 앞선 호출만 참조한다. 같은 Step의 앞선 Tool도 참조할 수 있다.
- 조건부 Tool 출력을 참조할 때는 해당 Tool이 생략되는 경우에도 연결이 유효해야 한다.
- 생략한 인자는 Tool에 정의된 기본값을 사용한다. 기본값 없는 필수 인자는 명시해야 한다.
- `source: input`은 해당 인자가 사용자에게서 제공받아야 하는 값임을 나타낸다. 질문에서 이미 제공되었는지, 화면에서 추가로 입력받아야 하는지는 실행 단계에서 판단한다.

## 5. 예시 파일

| 파일 | 내용 |
|---|---|
| [workflow_static.example.json](workflow_static.example.json) | 데이터 1개를 로드해 표본 추출·통계·히스토그램을 수행하는 static 예시 |
| [workflow_adaptive.example.json](workflow_adaptive.example.json) | 데이터 2개를 로드·결합해 표본 통계와 조건부 분석을 수행하는 adaptive 예시 |
