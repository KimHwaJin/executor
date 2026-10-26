---
name: data_quality_check
description: 데이터 구조, 결측률, 기술 통계와 이상치 후보를 진단하는 workflow step을 설계할 때 사용하는 skill. profile_data, compute_statistics, detect_outliers의 실행 여부와 진단 결과의 후속 전달을 결정한다.
---

# Data Quality Check

## capabilities

- 데이터 행·열과 결측 상태 확인
- 컬럼별 기술 통계 계산
- 수치형 컬럼의 이상치 후보 진단

## limitations

- 원본 데이터를 변경하지 않음
- 이상치 후보를 자동 제거하지 않음

## when_to_use

- 데이터 로드 또는 준비 후 품질 상태를 진단할 때
- 정제 Tool 실행 여부의 근거가 필요할 때

## not_for

- 결측치 대체, 이상치 제거 또는 모델 학습

## typical_previous_skill

- `data_load`
- `dataset_preparation`

## typical_next_skills

- `eda_analysis`
- `data_cleaning_pipeline`

## Tool list

| Tool | 실행 방식 | 실행 조건 | condition_tool | 역할 |
|---|---|---|---|---|
| `profile_data` | 항상 실행 | 입력 데이터가 준비된 경우 | 없음 | 기본 구조와 결측 상태 확인 |
| `compute_statistics` | 항상 실행 | 입력 데이터가 준비된 경우 | 없음 | 기술 통계 계산 |
| `detect_outliers` | 항상 실행 | 수치형 컬럼의 이상치 진단이 필요한 경우 | 없음 | 제거 전 이상치 후보 생성 |

