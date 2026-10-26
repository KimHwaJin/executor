---
name: data_cleaning_pipeline
description: 이상치 제거, 결측치 대체와 feature 선택을 조합하는 workflow step을 설계할 때 사용하는 skill. remove_outliers, impute_missing, select_features의 실행 조건, 순서와 모델링 데이터 흐름을 결정한다.
---

# Data Cleaning Pipeline

## capabilities

- 진단 단계에서 확인된 이상치 행 제거
- 결측값 대체
- 모델링용 feature 선택
- 다음 Skill에 전달할 단일 준비 데이터 구성

## limitations

- 이상치를 직접 탐지하지 않음
- 범주형 feature 인코딩이나 스케일링을 수행하지 않음
- 현재 feature 선택 Tool이 지원하는 데이터 타입 범위를 따른다.

## when_to_use

- 품질 진단 후 데이터를 실제로 정제할 때
- 지도학습용 feature subset을 만들 때

## not_for

- 원본 데이터 품질 진단이나 plot 생성

## typical_previous_skill

- `data_quality_check`
- `eda_analysis`

## typical_next_skills

- `predictive_modeling`

## Tool list

| Tool | 실행 방식 | 실행 조건 | condition_tool | 역할 |
|---|---|---|---|---|
| `remove_outliers` | 조건부 실행 | 이상치 index가 있는 경우 | `data_quality_check.detect_outliers` | 이상치 제거 |
| `impute_missing` | 조건부 실행 | 결측값이 있는 경우 | `data_quality_check.profile_data` | 결측값 대체 |
| `select_features` | 조건부 실행 | 지도학습용 feature 선택이 필요한 경우 | 없음 | feature와 target으로 모델 데이터 구성 |

