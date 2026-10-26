---
name: failure_group_comparison
description: 불량/정상 그룹 간 feature 분포를 통계적으로 비교하고 target-feature 연관도를 분석하는 workflow step을 설계할 때 사용하는 skill. compare_failure_normal, target_correlation의 실행 순서와 target 및 positive_label 전달을 결정한다.
---

# Failure Group Comparison

## capabilities

- 불량/정상 그룹 간 수치형 feature 분포 통계 비교 (Mann-Whitney U 검정, Cohen's d 효과 크기)
- target-feature 비선형 연관도 분석 (mutual_info, chi2, anova)

## limitations

- target을 이진 분류(positive_label vs others)로 변환하여 처리함
- 선형 상관분석은 `correlation_analysis` Tool(eda_analysis Skill)을 사용해야 함
- 결측치 대체, 이상치 제거, feature 변환 또는 모델 학습을 수행하지 않음
- plot 저장에는 실행 context의 output directory가 필요
- 시각화 Tool은 output_dir이 없으면 에러를 발생시킴

## when_to_use

- 불량/정상 그룹 간 유의미한 차이를 보이는 feature를 식별할 때
- target과 각 feature 간 비선형 연관도를 평가할 때

## not_for

- 전체 불량률 현황이나 시간/구간별 추이 분석 (`failure_analysis` Skill 사용)
- 선형 상관분석 (`eda_analysis` Skill의 `correlation_analysis` Tool 사용)
- 결측치 대체, 이상치 제거 또는 모델 학습

## typical_previous_skill

- `failure_analysis`
- `data_quality_check`
- `dataset_preparation`


## typical_next_skills

- `data_cleaning_pipeline`
- `predictive_modeling`

## Tool list

| Tool | 실행 방식 | 실행 조건 | condition_tool | 역할 |
|---|---|---|---|---|
| `compare_failure_normal` | 항상 실행 | target_column과 수치형 feature가 준비된 경우 | 없음 | 불량/정상 그룹 간 feature 통계 비교 |
| `target_correlation` | 항상 실행 | target_column과 feature가 준비된 경우 | 없음 | target-feature 비선형 연관도 분석 |

