---
name: failure_analysis
description: 불량률의 시간/구간별 추이와 클래스 분포를 분석하는 workflow step을 설계할 때 사용되는 skill. failure_rate_by_bin, failure_rate_trend, plot_failure_distribution의 선택, 실행 순서, target과 positive_label 전달, plot artifact 수집을 결정한다.
---

# Failure Analysis

## capabilities

- 수치형 feature 구간화별 불량률 분석 (equal_with, quantile)
- 시간/lot 순서별 불량률 추이 분석 (datetime 리샘플링, 숫자 순서 그룹화)
- 불량/정상 클래스 분포 시각화 (막대그래프, 파이차트)
- 추세 판단 (increasing, decreasing, stable)

## limitations

- target은 이진 분류(positive_label vs others)로 변환하여 처리함
- 결측치 대체, 이상치 제거, feature 변환 또는 모델 학습을 수행하지 않음
- plot 저장에는 실행 context의 output directory가 필요
- 시각화 Tool은 output_dir이 없으면 에러를 발생시킴

## when_to_use

- 데이터 준비 또는 품질 진단 후 불량률 현황과 추이를 파악할 때
- 수치형 구간, 시간 순서별로 불량률을 비교해야 할 때
- 불량/정상 클래스 불균형 정도를 확인할 때

## not_for

- 불량/정상 그룹 간 feature 분포 통계 비교 (`failure_group_comparison` Skill 사용)
- target-feature 연관도 분석 (`failure_group_comparison` Skill 사용)
- 결측치 대체, 이상치 제거 또는 모델 학습

## typical_previous_skill

- `data_quality_check`
- `dataset_preparation`
- `data_cleaning_pipeline`

## typical_next_skills

- `failure_group_comparison`
- `data_cleaning_pipeline`
- `predictive_modeling`

## Tool list

| Tool | 실행 방식 | 실행 조건 | condition_tool | 역할 |
|---|---|---|---|---|
| `plot_failure_distribution` | 항상 실행 | target_column이 확정된 경우 | 없음 | 불량/정상 클래스 분포 시각화 |
| `failure_rate_by_bin` | 조건부 실행 | 수치형 feature의 구간별 불량률 패턴이 필요한 경우 | `data_quality_check.compute_statistics` | 구간화별 불량률과 추세 분석 |
| `failure_rate_trend` | 조건부 실행 | 시간/lot 순서 컬럼이 있고 시계열 추이가 필요한 경우 | 없음 | 시간별 불량률 추이 분석 |
