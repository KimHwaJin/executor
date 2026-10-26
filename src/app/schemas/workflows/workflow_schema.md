# Workflow Schema 1.3

Workflow definition은 실행 계획만 표현한다. Runtime decision의 상태와 결과는 Workflow definition이 아니라 execution run state에서 관리한다.

## Core Rules

1. `schema_version`은 `"1.3"`만 사용한다.
2. `workflow.execution_mode`는 `static` 또는 `adaptive`이다.
3. `static` Workflow의 모든 Step과 Tool은 `execution: always`이다.
4. `adaptive` Workflow는 하나 이상의 `execution: conditional` Step 또는 Tool을 포함한다.
5. Workflow definition에는 `runtime_decisions`를 작성하지 않는다.
6. Workflow definition에는 Step/Tool `condition`을 작성하지 않는다. 값은 생략하거나 `null`로 둔다.
7. Skill을 선택하면 그 Skill의 `execution: always` Tool은 반드시 포함한다.
8. Skill의 `execution: conditional` Tool은 선택적이다.
9. `condition_tool`이 있는 conditional Tool을 포함하면 Workflow는 `adaptive`, 해당 Tool은 `conditional`이어야 한다.
10. `condition_tool`이 `없음`인 conditional Tool을 포함하면 생성 시점에 실행 확정된 것이므로 Workflow에서는 `always`로 작성한다.
11. `arguments`, `argument_sources`, `returns`는 모든 Tool에 작성한다. conditional Tool도 실행될 수 있으므로 동일하게 작성한다.

## Static Example

```yaml
schema_version: "1.3"
workflow:
  id: failure_prediction_static
  name: Failure prediction static workflow
  description: Merge data, split train/test, and train a model.
  goal: Train a failure prediction model.
  status: ready
  execution_mode: static
  input_schema: {}
  inputs: {}
  input_provenance: {}
  unresolved_inputs: []
  steps:
    - id: modeling
      order: 1
      skill: predictive_modeling
      skill_source: modeling/predictive_modeling.md
      depends_on: []
      execution: always
      condition: null
      tools:
        - id: split_dataset
          order: 1
          tool: split_dataset
          tool_source: app/workflow/tools/preprocessing/split_dataset.py
          selection_reason: Prepare train/test data.
          execution: always
          condition: null
          arguments: {}
          argument_sources: {}
          returns:
            result_variable: split_result
            outputs:
              train_data:
                selector: '["train_data"]'
                variable: train_data
      outputs:
        train_data: ${steps.modeling.tools.split_dataset.outputs.train_data}
  outputs:
    train_data: ${steps.modeling.outputs.train_data}
```

## Adaptive Definition Example

Workflow definition에는 조건 판단 후보만 표시한다.

```yaml
schema_version: "1.3"
workflow:
  id: eda_adaptive
  name: Adaptive EDA workflow
  description: Compute statistics and optionally run correlation analysis.
  goal: Run correlation only when the data supports it.
  status: ready
  execution_mode: adaptive
  input_schema: {}
  inputs: {}
  input_provenance: {}
  unresolved_inputs: []
  steps:
    - id: eda
      order: 1
      skill: eda_analysis
      skill_source: eda/eda_analysis.md
      depends_on: []
      execution: always
      condition: null
      tools:
        - id: eda_compute_statistics
          order: 1
          tool: compute_statistics
          tool_source: app/workflow/tools/eda/compute_statistics.py
          selection_reason: Required condition evidence for optional EDA tools.
          execution: always
          condition: null
          arguments: {}
          argument_sources: {}
          returns:
            result_variable: statistics_result
            outputs:
              statistics:
                selector: '$'
                variable: statistics_result
        - id: eda_correlation_analysis
          order: 2
          tool: correlation_analysis
          tool_source: app/workflow/tools/eda/correlation_analysis.py
          selection_reason: Run only if numeric columns are sufficient.
          execution: conditional
          condition: null
          arguments: {}
          argument_sources: {}
          returns:
            result_variable: correlation_result
            outputs:
              correlation:
                selector: '$'
                variable: correlation_result
      outputs:
        statistics: ${steps.eda.tools.eda_compute_statistics.outputs.statistics}
  outputs:
    statistics: ${steps.eda.outputs.statistics}
```

`eda_analysis.correlation_analysis`의 `condition_tool`은 Skill Index에서 `compute_statistics`로 조회한다. 실행 오케스트레이터는 Workflow의 `eda_compute_statistics` Tool을 먼저 실행하고, 그 관찰 결과를 바탕으로 `eda_correlation_analysis` 실행 여부를 execution run state에 기록한다.

## Execution Run State Example

```yaml
execution_run:
  workflow_id: eda_adaptive
  status: waiting_for_runtime_decision
  runtime_decisions:
    - id: decide_eda_correlation_analysis
      status: decided
      condition_tool_id: eda_compute_statistics
      candidate_tool_id: eda_correlation_analysis
      decision: run
      reason: 수치형 변수가 2개 이상 확인되어 상관분석을 실행합니다.
```

같은 Workflow라도 데이터가 바뀌면 execution run state의 decision은 달라질 수 있다.
