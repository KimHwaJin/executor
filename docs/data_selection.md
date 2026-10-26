> **이전 설계 문서 (현재 미사용)**
> 이 문서의 데이터를 먼저 선택하던 HITL, `SelectedDataset`, `DataSelectionResponse`, `WorkflowGenerationRequest`는 현재 코드에서 제거되었습니다.
> 현재 흐름은 워크플로우 후보 선택, Tool input 입력, 계획 승인 순서입니다. 최신 계약은 [workflow_run_frontend.md](workflow_run_frontend.md)와 [workflow_run_api.md](workflow_run_api.md)를 참고하세요. 아래 내용은 이전 설계 기록입니다.


# swagger UI에서 요청 양식
POST /{workflow}/api/data-selection
Content-Type: application/json

{
  "user_id": "mock-user-001",
  "project_id": "mock-project-001",
  "session_id": "mock-session-001",
  "decision": "approve",
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


# fastapi endpoint
@api_router.post(
    "/{workflow}/api/data-selection",
    summary="분석 데이터 선택",
)
async def select_data(
    workflow: str,
    request: DataSelectionRequest,
):
    selection = {
        "data_count": len(request.datasets),
        "datasets": [
            dataset.model_dump(mode="json")
            for dataset in request.datasets
        ],
    }

    # 동일 session의 HITL graph 재개
    result = await graph.ainvoke(
        Command(resume=selection),
        config={
            "configurable": {
                "thread_id": request.session_id,
            }
        },
    )

    return result

# hitl input
{
    "data_count": 2,
    "datasets": [
        {
            "data": "x",
            "data_type": "nce",
            "lot_cd": "6E2",
            "process": ["ALL"],
            "query_mode": "period",
            "start_dt": "2026-05-01",
            "end_dt": "2026-05-10",
            "limit": 100000000000,
            "transform_op": "pivot",
        },
        {
            "data": "y",
            "data_type": "wt_symbol",
            "lot_cd": "6E2",
            "process": ["PT1H"],
            "query_mode": "period",
            "start_dt": "2026-08-01",
            "end_dt": "2026-08-01",
            "limit": 1000001,
            "transform_op": "wt_fail_pivot",
        },
    ],
}

## pydantic 스키마
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.agents.orchestration_schema import DataSelectionResponse


class DataSelectionResumeRequest(BaseModel):
    user_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)

    # 데이터 선택 HITL에 그대로 전달할 값
    data_selection: DataSelectionResponse



# 데이터 타입 추가되면 다음을 확장
orchestration_schema.py (line 43)
class SelectedDataset(StrictModel):
    role: Literal["x", "y"]
    data_type: Literal["nce", "wt_symbol"]

data_load_steps.py (line 17)
TRANSFORM_BY_DATA_TYPE = {
    "nce": "transform_nce",
    "wt_symbol": "transform_wt",
}