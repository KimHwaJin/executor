> **이전 설계 문서 (현재 미사용)**
> 이 문서의 데이터를 먼저 선택하던 HITL, `SelectedDataset`, `DataSelectionResponse`, `WorkflowGenerationRequest`는 현재 코드에서 제거되었습니다.
> 현재 흐름은 워크플로우 후보 선택, Tool input 입력, 계획 승인 순서입니다. 최신 계약은 [workflow_run_frontend.md](workflow_run_frontend.md)와 [workflow_run_api.md](workflow_run_api.md)를 참고하세요. 아래 내용은 이전 설계 기록입니다.

# 데이터 선택 API 및 HITL 계약

## 1. 개요

데이터 선택 과정은 다음 순서로 진행한다.

```text
분석 요청
→ LangGraph 데이터 선택 단계에서 HITL interrupt
→ UI 또는 Swagger에서 데이터 선택값 제출
→ 서버가 DataSelectionResponse로 검증
→ 동일한 session/thread의 LangGraph를 resume
→ DATA_MOCK 설정에 따라 데이터 준비
→ 분석 Workflow 진행
```

`data_selection` 내부 값은 데이터 선택 HITL에 전달되는 값과 동일하다. Swagger
요청에는 사용자와 실행 세션을 식별하기 위한 외부 envelope가 추가된다.

> 현재 LangGraph Studio의 데이터 선택 HITL은 구현되어 있다. 이 문서의 Swagger 전용
> `/api/data-selection` endpoint와 `DataSelectionResumeRequest`는 추가 구현을 위한
> 계약안이며 아직 프로젝트에 추가되지 않았다.

## 2. Pydantic 스키마

데이터 선택 도메인 모델은
`src/app/schemas/agents/orchestration_schema.py`에 정의된 모델을 사용한다.

```python
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SelectedDataset(StrictModel):
    role: Literal["x", "y"]
    data_type: Literal["nce", "wt_symbol"]
    lot_cd: str = Field(min_length=1)
    process: list[str] = Field(min_length=1)
    query_mode: str = Field(min_length=1)
    start_dt: date
    end_dt: date
    limit: int = Field(gt=0)
    transform_op: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dataset(self) -> "SelectedDataset":
        if self.end_dt < self.start_dt:
            raise ValueError("end_dt must be on or after start_dt")

        expected_transform = {
            "nce": "pivot",
            "wt_symbol": "wt_fail_pivot",
        }[self.data_type]
        if self.transform_op != expected_transform:
            raise ValueError(
                f"{self.data_type} transform_op must be {expected_transform}"
            )
        return self


class DataSelectionResponse(StrictModel):
    data_count: int = Field(gt=0)
    datasets: list[SelectedDataset] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_selection(self) -> "DataSelectionResponse":
        if self.data_count != len(self.datasets):
            raise ValueError("data_count must equal the number of datasets")
        if {dataset.role for dataset in self.datasets} != {"x", "y"}:
            raise ValueError("analysis requires at least one X and one Y dataset")
        return self
```

Swagger 전용 요청 envelope는 다음과 같이 추가한다.

```python
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.agents.orchestration_schema import DataSelectionResponse


class DataSelectionResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    data_selection: DataSelectionResponse
```

## 3. Swagger Request Body

```json
{
  "user_id": "mock-user-001",
  "project_id": "mock-project-001",
  "session_id": "mock-session-001",
  "data_selection": {
    "data_count": 2,
    "datasets": [
      {
        "role": "x",
        "data_type": "nce",
        "lot_cd": "6E2",
        "process": ["ALL"],
        "query_mode": "period",
        "start_dt": "2026-05-01",
        "end_dt": "2026-05-10",
        "limit": 100000000000,
        "transform_op": "pivot"
      },
      {
        "role": "y",
        "data_type": "wt_symbol",
        "lot_cd": "6E2",
        "process": ["PT1H"],
        "query_mode": "period",
        "start_dt": "2026-08-01",
        "end_dt": "2026-08-01",
        "limit": 1000001,
        "transform_op": "wt_fail_pivot"
      }
    ]
  }
}
```

외부 envelope 필드의 역할은 다음과 같다.

| 필드 | 설명 |
|---|---|
| `user_id` | 요청 사용자 식별자 |
| `project_id` | 프로젝트 식별자 |
| `session_id` | 중단된 LangGraph 실행을 찾기 위한 세션 식별자 |
| `data_selection` | 데이터 선택 HITL에 전달할 도메인 값 |

## 4. FastAPI endpoint 구현안

```python
from fastapi import HTTPException
from langgraph.types import Command

from agent_config import build_langgraph_thread_id


@api_router.post(
    "/{workflow}/api/data-selection",
    summary="분석 데이터 선택 HITL 재개",
)
async def select_data(
    workflow: str,
    request: DataSelectionResumeRequest,
):
    graph = _get_graph_or_404(workflow)
    selection = request.data_selection.model_dump(mode="json")
    thread_id = build_langgraph_thread_id(request.session_id)

    try:
        result = await graph.ainvoke(
            Command(resume=selection),
            config={
                "configurable": {
                    "thread_id": thread_id,
                }
            },
        )
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=f"데이터 선택 이후 graph 재개에 실패했습니다: {error}",
        ) from error

    return result
```

실제 서비스 구현 시에는 요청의 `user_id`, `project_id`, `session_id`가 중단된
thread의 소유 정보와 일치하는지도 확인해야 한다.

## 5. HITL에 들어가는 구조

FastAPI endpoint는 Swagger 요청 전체가 아니라 `data_selection`만 LangGraph에
전달한다.

```python
selection = request.data_selection.model_dump(mode="json")
Command(resume=selection)
```

실제 HITL resume 값은 다음과 같다.

```json
{
  "data_count": 2,
  "datasets": [
    {
      "role": "x",
      "data_type": "nce",
      "lot_cd": "6E2",
      "process": ["ALL"],
      "query_mode": "period",
      "start_dt": "2026-05-01",
      "end_dt": "2026-05-10",
      "limit": 100000000000,
      "transform_op": "pivot"
    },
    {
      "role": "y",
      "data_type": "wt_symbol",
      "lot_cd": "6E2",
      "process": ["PT1H"],
      "query_mode": "period",
      "start_dt": "2026-08-01",
      "end_dt": "2026-08-01",
      "limit": 1000001,
      "transform_op": "wt_fail_pivot"
    }
  ]
}
```

## 6. Swagger 요청과 HITL 값의 관계

```text
Swagger Request Body
├─ user_id
├─ project_id
├─ session_id
└─ data_selection ───────────────┐
                                 ↓
                       LangGraph HITL resume 값
                       ├─ data_count
                       └─ datasets
```

- Swagger Request Body 전체와 HITL 값 전체는 동일하지 않다.
- Swagger의 `data_selection` 값과 HITL resume 값은 동일하다.
- `data_count`는 `len(datasets)`와 반드시 같아야 한다.
- 각 dataset의 `role`은 `x` 또는 `y`이다.
- 현재 `data_type`은 `nce`, `wt_symbol`만 허용한다.

## 7. 데이터 준비 분기

데이터 선택값이 검증된 이후 `DATA_MOCK` 설정에 따라 선행 Workflow가 달라진다.

```text
DATA_MOCK=false
├─ extract_data
└─ transform_nce 또는 transform_wt

DATA_MOCK=true
└─ data_load로 준비된 wide parquet 직접 로드
```

mock 파일 경로는 다음 규칙으로 결정한다.

```python
Path(EXECUTOR_SHARED_INPUT_ROOT) / "data" / MOCK_DATA_PATH_BY_TYPE[data_type]
```

기본 설정의 결과는 다음과 같다.

```text
/workspace/pv/data/df_nce_wide_format.parquet
/workspace/pv/data/df_wt_symbol_wide_format.parquet
```