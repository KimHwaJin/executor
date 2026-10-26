# Workflow 1.0 컴파일러 사용법

`app.agents.workflow_generator.workflow_standard_compiler`는 작성용 Workflow 1.0을 기존 Compiled Workflow 1.3으로 변환한다. 컴파일러 자체는 LLM 호출이나 저장·실행을 하지 않는다. 분석 그래프는 생성 검증, 후보 선택, 실행 입력 연결에 이 컴파일러를 사용한다. 이전 Plan 컴파일러는 기존 호출자를 위해 남겨둔다.

## API

```python
from app.agents.workflow_generator.workflow_standard_compiler import (
    extract_workflow_inputs,
    compile_workflow,
)
from app.services.rule_based_notebook_generator import APP_SOURCE_ROOT, build_notebook

# document: 작성용 Workflow JSON을 읽은 dict
fields = extract_workflow_inputs(document)

# DB 등록 시 발급받은 UUID를 전달한다. 컴파일러가 새 UUID를 만들지 않는다.
workflow_id = "00000000-0000-0000-0000-000000000001"
pending = compile_workflow(document, workflow_id=workflow_id)
# input이 미충족이면 pending['workflow']['status'] == 'needs_input'

# 아래는 실행 입력 한 항목의 형식. 실제로는 fields의 필요한 항목을 모두 제공한다.
execution_inputs = [
    {
        "step_index": 0,
        "tool_index": 0,
        "parameter": "data_type",
        "value": "nce",
        "origin": "user_answer",  # 또는 user_request
        "confirmed": True,
    }
]
compiled = compile_workflow(
    document,
    workflow_id=workflow_id,
    execution_inputs=execution_inputs,
)
if compiled["workflow"]["status"] == "ready":
    notebook = build_notebook(compiled, project_root=APP_SOURCE_ROOT)
```

`origin` 기본값은 `user_answer`, `confirmed` 기본값은 `True`다. 질문에서 추출했지만 아직 확인하지 않은 값은 `origin: user_request`, `confirmed: false`로 전달하면 미확정 입력으로 남는다. 중복 항목과 `source: input`으로 선언되지 않은 항목은 오류다. 원본 Workflow와 전달한 입력 객체는 수정하지 않는다.

## 입력과 변환 규칙

- 입력은 `(step_index, tool_index, parameter)`로 구분한다. 동일 이름의 파라미터를 호출 사이에서 공유하지 않는다.
- `extract_workflow_inputs`는 주소, Tool 이름, Registry 타입, nullable 및 기본값 메타데이터를 반환한다. 기본값은 화면 참고용이며 `source: input` 값으로 자동 적용하지 않는다.
- `int | float`, `list | str | None` 등의 타입을 보존하며 문자열 숫자를 자동 변환하지 않는다. Registry가 `list`만 선언하면 원소 타입을 추측하지 않는다.
- 누락값과 명시적인 `null`은 다르다. Registry의 nullable/default=None 계약이 허용하는 `null`은 확정값으로 사용할 수 있다. 1.3의 required/null 제약 때문에 해당 내부 input_schema는 required=false로 표현하되 입력 존재 여부는 컴파일 단계에서 별도로 검사한다.
- literal은 지정값을 보존하고, 생략 인자는 Registry 기본값을 사용한다. 기본값 없는 일반 인자는 오류다. 미지정 `**kwargs`는 함수 호출에 전달하지 않는다.
- 호출 위치로 ID·순서를 생성하며 데이터 로드 Step을 자동 삽입하지 않는다. NCE/WT 변환은 선행 extract_data.data를 참조해야 하며 실제 data_type과 맞는지 검사한다.
- Workflow ID는 전달받은 UUID, description/goal은 user_request로 채운다. actor와 즉각 실행 플래그는 내부 context.authoring에 보존한다. 즉각 실행 여부를 결정하는 로직은 없다.
- Step 출력 별칭과 Python 변수명을 호출 위치별로 생성한다. 최종 출력은 항상 실행되는 Tool의 등록 출력으로 구성한다.

## Adaptive

선언된 execution_mode/Tool execution과 Skill 규칙이 맞아야 한다. 런타임과 동일하게 가장 가까운 선행 condition Tool을 찾고, 항상 실행되는 진단 Tool인지 검증한다. 조건 Tool과 대상 Tool의 data 참조가 다른 경우에는 보수적으로 오류 처리한다. 따라서 다른 참조를 거쳐 같은 데이터로 귀결되는 복잡한 정제 체인은 별도 계보 검증 확장이 필요하다.

생략될 수 있는 조건부 출력을 후속 Tool이 요구하면 거부한다. 기존 셀 생성기가 지원하는 data passthrough 출력은 허용한다. 실행 중 include/exclude 결정은 이 컴파일러의 범위 밖이다.

## 검증 및 실행 범위

`src/app/test/fixtures/workflow_standard`에는 검토용 예시의 테스트 스냅샷이 있다. Static 데이터 1개, Adaptive 데이터 2개 및 모든 조건부 실행/생략 조합의 셀 생성·Python 구문을 검증한다. 실제 외부 데이터 조회나 실행기 호출을 수행하는 테스트는 아니다.

프로젝트 루트에서 다음과 같이 테스트한다. 루트의 app.py와 src/app 패키지 충돌을 피하려고 src를 모듈 검색 경로 맨 앞에 둔다.

```powershell
..\venv311\Scripts\python.exe -c 'import sys, unittest; sys.path.insert(0,"src"); suite=unittest.defaultTestLoader.loadTestsFromName("app.test.test_workflow_standard_compiler"); result=unittest.TextTestRunner().run(suite); sys.exit(not result.wasSuccessful())'
```

compute_statistics는 output_dir이 지정되면 통계 JSON을 해당 경로에 저장한다. 기본값 None에서는 기존처럼 파일을 저장하지 않고 statistics를 반환한다. 기존 셀 생성기와 1.3 스키마는 변경하지 않았다.

## 통합 변환 Tool 테스트

`transform_data(data_type, data=None, transform_op=None, mock_data_dir="/workspace/pv/data")`는 현업의 통합 변환 구현 전 테스트용이다. `nce`와 `wt_symbol`을 지원하고, 이미 wide 형식인 입력은 유지하며 나머지는 해당 mock Parquet을 읽는다. 실제 pivot 로직은 아직 구현하지 않는다. `transform_op`은 테스트 메타데이터로 기록되며 생략 시 각각 `pivot`, `wt_fail_pivot`을 사용한다.

기존 transform_nce/transform_wt는 tmp로 이동되어 현재 Registry에서 제외되어 있다. 새 Workflow에서 transform_data를 선택할 때 data와 data_type 모두 같은 extract_data 호출의 출력으로 연결하면 된다.

```json
{
  "tool": "transform_data",
  "execution": "always",
  "arguments": {
    "data": {"source": "tool_output", "step_index": 0, "tool_index": 0, "output": "data"},
    "data_type": {"source": "tool_output", "step_index": 0, "tool_index": 0, "output": "data_type"},
    "transform_op": {"source": "input"}
  }
}
```

로컬 mock 파일 경로를 사용하려면 mock_data_dir 인자에 literal 경로를 명시한다. Registry·Skill Index에 등록했으며 셀 생성기는 통합 Tool의 전체 데이터 반환값을 출력하지 않는다.
