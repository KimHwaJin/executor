---
name: failure_group_comparison
description: 불량/정상 그룹 간 feature 분포를 통계적으로 비교하고 target-feature 연관도를 분석하는 workflow step을 설계할 때 사용하는 skill. compare_failure_normal, target_correlation, plot_failure_by_group, plot_failure_distribution_by_feature의 선택, 실행 순서, target과 positive_label 전달, plot_artifact 수집을 결정한다.
---

# Failure Group Comparison

## capabilities

- 불량/정상 그룹 간 수치형 feature 분포 통계 비교 (Mann-Whitney U 검정, Cohen's d 효과 크기)
- target-feature 비선형 연관도 분석 (mutual_info, chi2, anova)
- 범주형 그룹별 불량률 시각화 (바 플롯, 전체 불량률 기준선)
- feature별 불량/정상 분포 시각화 (boxplot, violin, histogram, kde)

## limitations

- target을 이진 분류(positive_label vs others)로 변환하여 처리함
- 선형 상관분석은 `correlation_analysis` Tool(eda_analysis Skill)을 사용해야 함
- 결측치 대체, 이상치 제거, feature 변환 또는 모델 학습을 수행하지 않음
- plot 저장에는 실행 context의 output directory가 필요
- 시각화 Tool은 output_dir이 없으면 에러를 발생시킴

## when_to_use

- 불량/정상 그룹 간 유의미한 차이를 보이는 feature를 식별할 때
- target과 각 feature 간 비선형 연관도를 평가할 때
- 범주형 그룹별 불량률 차이를 시각화할 때
- feature별 불량/정상 분포 형태를 비교할 때

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
| `plot_failure_by_group` | 조건부 실행 | 범주형 group_column이 있고 그룹별 불량률 시각화가 필요한 경우 | 없음 | 그룹별 불량률 바 플롯 |
| `plot_failure_distribution_by_feature` | 조건부 실행 | 수치형 feature별 불량/정상 분포 시각화가 필요한 경우 | `data_quality_check.compute_statistics` | feature별 분포 plot (boxplot/violin/histogram/kde) |
