# Tool Registry 작성 스키마

이 문서는 `app/workflow/tools/tool_registry.yaml`의 구조를 정의한다.
Registry는 Python Tool 파일에서 규칙 기반으로 생성하며, 함수 코드에서 확인되는 정보만 기록한다.

## 작성 원칙

1. Registry에 등록된 Tool은 모두 Workflow에서 사용할 수 있다.
2. Tool ID, Python 파일명과 `function_name`은 동일하다.
3. `source`는 `app/workflow/tools` 기준 상대 경로다.
4. 함수 모듈을 import하거나 실행하지 않고 Python AST로 읽는다.
5. 함수 시그니처, 인자, 기본값, docstring, 반환 annotation과 고정 반환 key만 기록한다.
6. 파일명과 같은 이름의 함수가 없는 Python 파일은 Registry에 등록하지 않는다.

## Registry 양식

```yaml
schema_version: "2.0"
registry_type: tool_registry
description: Python Tool 파일에서 AST로 추출한 함수 호출 정보

generation:
  method: python_ast
  llm_used: false
  source_root: app/workflow/tools

tools:
  tool_name:
    category: category_name
    function_name: tool_name
    source: category_name/tool_name.py
    signature: "tool_name(data: object = None) -> dict[str, object]"
    docstring: |-
      Tool 함수의 docstring 전체 내용
    inputs:
      data:
        type: object
        kind: positional_or_keyword
        has_default: true
        default: null
    returns:
      type: dict[str, object]
      outputs:
        result:
          selector: '["result"]'
    packages: [pandas]
```

## Input 필드

| 필드 | 의미 |
|---|---|
| `type` | 함수 parameter의 type annotation. annotation이 없으면 `null` |
| `kind` | `positional_only`, `positional_or_keyword`, `keyword_only`, `var_positional`, `var_keyword` 중 하나 |
| `has_default` | 함수 시그니처에 기본값이 있는지 여부 |
| `default` | 함수 시그니처에 작성된 기본값 |

## Return 필드

| 필드 | 의미 |
|---|---|
| `type` | 함수 return annotation. annotation이 없으면 `null` |
| `outputs` | 함수의 고정 dictionary 반환 key 또는 단일 반환값 |
| `selector` | dictionary key 접근 selector. 단일 직접 반환은 `$` |

고정 dictionary 반환:

```python
return {"data": data, "summary": summary}
```

```yaml
returns:
  type: dict[str, object]
  outputs:
    data:
      selector: '["data"]'
    summary:
      selector: '["summary"]'
```

단일 직접 반환:

```python
return dataframe
```

```yaml
returns:
  type: null
  outputs:
    result:
      selector: "$"
```

## 생성 명령

```powershell
..\venv311\Scripts\python.exe app\workflow\tools\generate_tool_registry.py
```
