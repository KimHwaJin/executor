---
name: eda_analysis
description: 데이터 분포와 변수 관계를 탐색하는 workflow step을 설계할 때 사용하는 skill. compute_statistics를 먼저 실행해 컬럼 특성을 파악하고 histogram_eda, boxplot_eda, correlation_analysis의 조건부 실행을 결정한다.
---

# EDA Analysis

## capabilities

- 수치형 분포와 범주형 빈도 시각화
- 수치형 컬럼의 boxplot 생성
- 변수 간 및 target과의 상관관계 분석
- 기술 통계 결과를 근거로 조건부 EDA Tool 선택

## limitations

- 품질 진단이나 데이터 정제를 수행하지 않음
- plot 저장에는 실행 context의 output directory가 필요

## when_to_use

- 데이터 품질 확인 후 분포나 변수 관계를 탐색할 때

## not_for

- 결측치 대체, 이상치 제거 또는 모델 학습

## typical_previous_skill

- `data_quality_check`

## typical_next_skills

- `data_cleaning_pipeline`
- `predictive_modeling`

## Tool list

| Tool | 실행 방식 | 실행 조건 | condition_tool | 역할 |
|---|---|---|---|---|
| `compute_statistics` | 항상 실행 | 입력 데이터가 준비된 경우 | 없음 | 컬럼별 기술 통계를 계산하고 후속 Tool 선택 근거 생성 |
| `histogram_eda` | 항상 실행 | 입력 데이터가 준비된 경우 | 없음 | 컬럼별 분포 이미지 생성 |
| `boxplot_eda` | 조건부 실행 | 수치형 산포와 이상치 형태를 볼 경우 | `compute_statistics` | 수치형 boxplot 생성 |
| `correlation_analysis` | 조건부 실행 | 수치형 변수 2개 이상이 확인된 경우 | `compute_statistics` | 상관관계와 유의성 분석 |

